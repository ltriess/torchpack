import time
from typing import Any, Dict, List, Optional

from torch.utils.data import DataLoader, DistributedSampler

from torchpack.callbacks import (
    Callback,
    Callbacks,
    ConsoleWriter,
    EstimatedTimeLeft,
    JSONLWriter,
    MetaInfoSaver,
    ProgressBar,
    TFEventWriter,
)
from torchpack.train.exception import StopTraining
from torchpack.train.summary import Summary
from torchpack.utils import humanize
from torchpack.utils.logging import logger

__all__ = ["Trainer"]


class Trainer:
    """
    Base class for a trainer.
    """

    def train_with_defaults(
        self,
        dataflow: DataLoader,
        *,
        num_epochs: int = 9999999,
        eval_interval: int = None,
        splits: List[str] = None,
        callbacks: Optional[List[Callback]] = None
    ) -> None:
        if callbacks is None:
            callbacks = []
        callbacks += [
            MetaInfoSaver(),
            ConsoleWriter(),
            JSONLWriter(),
            ProgressBar(),
            EstimatedTimeLeft(),
        ]
        if splits is None:
            callbacks.append(TFEventWriter())
        else:
            callbacks += [TFEventWriter(split=s) for s in splits]

        self.train(
            dataflow=dataflow,
            num_epochs=num_epochs,
            eval_interval=eval_interval,
            splits=splits,
            callbacks=callbacks,
        )

    def train(
        self,
        dataflow: DataLoader,
        *,
        num_epochs: int = 9999999,
        eval_interval: int = None,
        splits: List[str] = None,
        callbacks: Optional[List[Callback]] = None
    ) -> None:
        self.dataflow = dataflow
        self.steps_per_epoch = len(self.dataflow)
        self.num_epochs = num_epochs

        if callbacks is None:
            callbacks = []
        self.callbacks = Callbacks(callbacks)
        if splits is None:
            self.summary = {"0": Summary()}
        else:
            self.summary = {s: Summary(split=s) for s in splits}

        try:
            self.callbacks.set_trainer(self)
            for s in self.summary.values():
                s.set_trainer(self)

            self.epoch_num = 0
            self.global_step = 0

            train_time = time.perf_counter()
            self.before_train()

            while self.epoch_num < self.num_epochs:
                self.epoch_num += 1
                self.local_step = 0

                logger.info(
                    "Epoch {}/{} started.".format(self.epoch_num, self.num_epochs)
                )
                epoch_time = time.perf_counter()
                self.before_epoch()

                for feed_dict in self.dataflow:
                    self.local_step += 1
                    self.global_step += 1

                    self.before_step(feed_dict)
                    output_dict = self.run_step(feed_dict)
                    self.after_step(output_dict)

                    self.trigger_step()

                self.after_epoch()
                logger.info(
                    "Training finished in {}.".format(
                        humanize.naturaldelta(time.perf_counter() - epoch_time)
                    )
                )

                if eval_interval is not None:
                    if self.epoch_num % eval_interval == 0:
                        self.trigger_epoch()
                else:
                    self.trigger_epoch()
                logger.info(
                    "Epoch finished in {}.".format(
                        humanize.naturaldelta(time.perf_counter() - epoch_time)
                    )
                )

            logger.success(
                "{} epochs of training finished in {}.".format(
                    self.num_epochs,
                    humanize.naturaldelta(time.perf_counter() - train_time),
                )
            )
        except StopTraining as e:
            logger.info("Training was stopped by {}.".format(str(e)))
        finally:
            self.after_train()

    def before_train(self) -> None:
        self._before_train()
        self.callbacks.before_train()

    def _before_train(self) -> None:
        pass

    def before_epoch(self) -> None:
        if isinstance(self.dataflow, DataLoader) and isinstance(
            self.dataflow.sampler, DistributedSampler
        ):
            self.dataflow.sampler.set_epoch(self.epoch_num)
        self._before_epoch()
        self.callbacks.before_epoch()

    def _before_epoch(self) -> None:
        pass

    def before_step(self, feed_dict: Dict[str, Any]) -> None:
        self._before_step(feed_dict)
        self.callbacks.before_step(feed_dict)

    def _before_step(self, feed_dict: Dict[str, Any]) -> None:
        pass

    def run_step(self, feed_dict: Dict[str, Any]) -> Dict[str, Any]:
        output_dict = self._run_step(feed_dict)
        return output_dict

    def _run_step(self, feed_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Defines what to do in one iteration.
        """
        raise NotImplementedError

    def after_step(self, output_dict: Dict[str, Any]) -> None:
        self.callbacks.after_step(output_dict)
        self._after_step(output_dict)

    def _after_step(self, output_dict: Dict[str, Any]) -> None:
        pass

    def trigger_step(self) -> None:
        self.callbacks.trigger_step()
        self._trigger_step()

    def _trigger_step(self) -> None:
        pass

    def after_epoch(self) -> None:
        self.callbacks.after_epoch()
        self._after_epoch()

    def _after_epoch(self) -> None:
        pass

    def trigger_epoch(self) -> None:
        self.callbacks.trigger_epoch()
        self._trigger_epoch()

    def _trigger_epoch(self) -> None:
        pass

    def after_train(self) -> None:
        self.callbacks.after_train()
        self._after_train()

    def _after_train(self) -> None:
        pass

    def state_dict(self) -> Dict[str, Any]:
        state_dict = self._state_dict()
        state_dict["callbacks"] = self.callbacks.state_dict()
        state_dict["epoch_num"] = self.epoch_num
        state_dict["local_step"] = self.local_step
        state_dict["global_step"] = self.global_step
        return state_dict

    def _state_dict(self) -> Dict[str, Any]:
        return dict()

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        self.epoch_num = state_dict.pop("epoch_num")
        self.local_step = state_dict.pop("local_step")
        self.global_step = state_dict.pop("global_step")
        self.callbacks.load_state_dict(state_dict.pop("callbacks"))
        self._load_state_dict(state_dict)

    def _load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        pass
