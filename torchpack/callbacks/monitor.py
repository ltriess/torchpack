import json
import operator
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime

import numpy as np
from tensorboardX import FileWriter
from tensorboardX.summary import scalar, image

from torchpack.callbacks.callback import Callback
from torchpack.utils.logging import logger, get_logger_dir

__all__ = ['Monitor', 'Monitors', 'TFEventWriter', 'JSONWriter', 'ScalarPrinter']


class Monitor(Callback):
    """
    Base class for monitors which monitor a training progress,
    by processing different types of summary/statistics from trainer.
    """

    master_only = False

    def add_scalar(self, name, val):
        if isinstance(val, np.integer):
            val = int(val)
        if isinstance(val, np.floating):
            val = float(val)

        self._add_scalar(name, val)

    def _add_scalar(self, name, val):
        pass

    def add_image(self, name, val):
        assert isinstance(val, np.ndarray), type(val)

        # todo: double check whether transform is correct
        if val.ndim == 2:
            val = val[np.newaxis, :, :, np.newaxis]
        elif val.ndim == 3:
            if val.shape[-1] in [1, 3, 4]:
                val = val[np.newaxis, ...]
            else:
                val = val[..., np.newaxis]
        assert val.ndim == 4, val.shape

        self._add_image(name, val)

    def _add_image(self, name, val):
        pass

    def _add_summary(self, summary):
        pass

    def add_event(self, event):
        pass


class Monitors(Monitor):
    """
    A container to hold all monitors.
    """

    def __init__(self, monitors):
        for monitor in monitors:
            assert isinstance(monitor, Monitor), type(monitor)
        self.monitors = monitors
        self.scalars = defaultdict(list)

    def _set_trainer(self, trainer):
        for monitor in self.monitors:
            monitor.set_trainer(trainer)

    def _before_train(self):
        for monitor in self.monitors:
            monitor.before_train()

    def _after_train(self):
        for monitor in self.monitors:
            monitor.after_train()

    def _before_epoch(self):
        for monitor in self.monitors:
            monitor.before_epoch()

    def _after_epoch(self):
        for monitor in self.monitors:
            monitor.after_epoch()

    def _before_step(self, *args, **kwargs):
        for monitor in self.monitors:
            monitor.before_step(*args, **kwargs)

    def _after_step(self, *args, **kwargs):
        for monitor in self.monitors:
            monitor.after_step(*args, **kwargs)

    def _trigger_epoch(self):
        for monitor in self.monitors:
            monitor.trigger_epoch()

    def _trigger_step(self):
        for monitor in self.monitors:
            monitor.trigger_step()

    def _trigger(self):
        for monitor in self.monitors:
            monitor.trigger()

    def _add_scalar(self, name, val):
        self.scalars[name].append((self.trainer.global_step, val))
        for monitor in self.monitors:
            monitor.add_scalar(name, val)

    def _add_image(self, tag, val):
        for monitor in self.monitors:
            monitor.add_image(tag, val)

    def get_latest(self, name):
        return self.scalars[name][-1][1]

    def get_history(self, name):
        return self.scalars[name]


class TFEventWriter(Monitor):
    """
    Write summaries to TensorFlow event file.
    """

    def __init__(self, logdir=None, max_queue=10, flush_secs=120):
        """
        Args:
            logdir: ``logger.get_logger_dir()`` by default.
            max_queue, flush_secs: Same as in :class:`tf.summary.FileWriter`.
        """
        if logdir is None:
            logdir = get_logger_dir()
        self.logdir = logdir
        self.max_queue = max_queue
        self.flush_secs = flush_secs

    def _before_train(self):
        self.writer = FileWriter(self.logdir, max_queue=self.max_queue, flush_secs=self.flush_secs)

    def _trigger_epoch(self):
        self._trigger()

    def _trigger(self):
        self.writer.flush()

    def _after_train(self):
        self.writer.close()

    def _add_summary(self, summary):
        self.writer.add_summary(summary, self.trainer.global_step)

    def _add_scalar(self, name, val):
        self._add_summary(scalar(name, val))

    def _add_image(self, name, val):
        self._add_summary(image(name, val))


class JSONWriter(Monitor):
    """
    Write all scalar data to a json file under ``logger.get_logger_dir()``, grouped by their global step.
    If found an earlier json history file, will append to it.
    """

    FILENAME = 'stats.json'
    """
    The name of the json file. Do not change it.
    """

    @staticmethod
    def load_existing_json():
        """
        Look for an existing json under :meth:`logger.get_logger_dir()` named "stats.json",
        and return the loaded list of statistics if found. Returns None otherwise.
        """
        dir = get_logger_dir()
        fname = os.path.join(dir, JSONWriter.FILENAME)
        if os.path.exists(fname):
            with open(fname) as f:
                stats = json.load(f)
                assert isinstance(stats, list), type(stats)
                return stats
        return None

    @staticmethod
    def load_existing_epoch_number():
        """
        Try to load the latest epoch number from an existing json stats file (if any).
        Returns None if not found.
        """
        stats = JSONWriter.load_existing_json()
        try:
            return int(stats[-1]['epoch_num'])
        except Exception:
            return None

    # initialize the stats here, because before_train from other callbacks may use it
    def _before_train(self):
        self._stats = []
        self._stat_now = {}
        self._last_gs = -1

        stats = JSONWriter.load_existing_json()
        self._fname = os.path.join(get_logger_dir(), JSONWriter.FILENAME)
        if stats is not None:
            try:
                epoch = stats[-1]['epoch_num'] + 1
            except Exception:
                epoch = None

            # check against the current training settings
            # therefore this logic needs to be in before_train stage
            starting_epoch = self.trainer.starting_epoch
            if epoch is None or epoch == starting_epoch:
                logger.info("Found existing JSON inside {}, will append to it.".format(get_logger_dir()))
                self._stats = stats
            else:
                logger.warning(
                    "History epoch={} from JSON is not the predecessor of the current starting_epoch={}".format(
                        epoch - 1, starting_epoch))
                logger.warning("If you want to resume old training, either use `AutoResumeTrainConfig` "
                               "or correctly set the new starting_epoch yourself to avoid inconsistency. ")

                backup_fname = JSONWriter.FILENAME + '.' + datetime.now().strftime('%m%d-%H%M%S')
                backup_fname = os.path.join(get_logger_dir(), backup_fname)

                logger.warn("Now, we will train with starting_epoch={} and backup old json to {}".format(
                    self.trainer.starting_epoch, backup_fname))
                shutil.move(self._fname, backup_fname)

        # in case we have something to log here.
        self._trigger()

    def _trigger_step(self):
        # will do this in trigger_epoch
        if self.trainer.local_step != self.trainer.steps_per_epoch - 1:
            self._trigger()

    def _trigger_epoch(self):
        self._trigger()

    def _trigger(self):
        """
        Add stats to json and dump to disk.
        Note that this method is idempotent.
        """
        if len(self._stat_now):
            self._stat_now['epoch_num'] = self.trainer.epoch_num
            self._stat_now['global_step'] = self.trainer.global_step

            self._stats.append(self._stat_now)
            self._stat_now = {}

            tmp_filename = self._fname + '.tmp'
            try:
                with open(tmp_filename, 'w') as f:
                    json.dump(self._stats, f)
                shutil.move(tmp_filename, self._fname)
            except IOError:  # disk error sometimes..
                logger.exception("Exception in JSONWriter._write_stat()!")

    def _add_scalar(self, name, val):
        self._stat_now[name] = float(val)


class ScalarPrinter(Monitor):
    """
    Print scalar data into terminal.
    """

    def __init__(self, trigger_epoch=True, trigger_step=False,
                 whitelist=None, blacklist=None):
        """
        Args:
            enable_step, enable_epoch (bool): whether to print the
                monitor data (if any) between steps or between epochs.
            whitelist (list[str] or None): A list of regex. Only names
                matching some regex will be allowed for printing.
                Defaults to match all names.
            blacklist (list[str] or None): A list of regex. Names matching
                any regex will not be printed. Defaults to match no names.
        """

        def compile_regex(rs):
            if rs is None:
                return None
            rs = set([re.compile(r) for r in rs])
            return rs

        self._whitelist = compile_regex(whitelist)
        if blacklist is None:
            blacklist = []
        self._blacklist = compile_regex(blacklist)

        self._enable_step = trigger_step
        self._enable_epoch = trigger_epoch
        self._dic = {}

    def _before_train(self):
        self._trigger()

    def _trigger_step(self):
        if self._enable_step:
            if self.trainer.local_step != self.trainer.steps_per_epoch - 1:
                # not the last step
                self._trigger()
            else:
                if not self._enable_epoch:
                    self._trigger()
                # otherwise, will print them together

    def _trigger_epoch(self):
        if self._enable_epoch:
            self._trigger()

    def _trigger(self):
        def match_regex_list(regexs, name):
            for r in regexs:
                if r.search(name) is not None:
                    return True
            return False

        texts = []
        for k, v in sorted(self._dic.items(), key=operator.itemgetter(0)):
            if self._whitelist is None or match_regex_list(self._whitelist, k):
                if not match_regex_list(self._blacklist, k):
                    texts.append('[{}] = {:.5g}'.format(k, v))

        if texts:
            logger.info('\n+ '.join([''] + texts))

        self._dic = {}

    def _add_scalar(self, name, val):
        self._dic[name] = float(val)
