"""
"""

import logging

from .storage.es_storage import ESStorage
from .storage.local_storage import LocalStorage
from .config import Configuration
from .model.som_model import SOMModel
from .model.model_exception import ModelLoadException, ModelSaveException
from .model.w2v_model import W2VModel

import os
import numpy as np
import datetime

import time
import matplotlib
matplotlib.use("agg")


_LOGGER = logging.getLogger(__name__)


class AnomalyDetector():
    """Implements training and inference of Self-Organizing Map to detect anomalies in logs."""

    STORAGE_BACKENDS = [LocalStorage, ESStorage]

    def __init__(self, configuration: Configuration):
        """Initialize the anomaly detector."""
        self.config = configuration
        self.update_model = os.path.isfile(configuration.MODEL_PATH) and configuration.TRAIN_UPDATE_MODEL  # model exists and update was requested
        self.update_w2v_model = os.path.isfile(configuration.W2V_MODEL_PATH) and configuration.TRAIN_UPDATE_MODEL  # model exists and update was requested
        self.recreate_models = False

        for backend in self.STORAGE_BACKENDS:
            if backend.NAME == self.config.STORAGE_BACKEND:
                _LOGGER.info("Using %s storage backend" % backend.NAME)
                self.storage = backend(configuration)
                break
        if not self.storage:
            raise Exception("Could not use %s storage backend" % self.STORAGE_BACKENDS)

        self.model = SOMModel()
        self.w2v_model = W2VModel()
        try:
            self.model.load(self.config.MODEL_PATH)
        except ModelLoadException as ex:
            _LOGGER.error("Failed to load SOM model: %s" % ex)
            self.update_model = False
            self.recreate_models = True

        try:
            self.w2v_model.load(self.config.W2V_MODEL_PATH)
        except ModelLoadException as ex:
            _LOGGER.error("Failed to load W2V model: %s" % ex)
            self.update_w2v_model = False
            self.recreate_models = True

    def _load_data(self, time_span, max_entries):
        data, raw = self.storage.retrieve(time_span, max_entries)

        if len(data) == 0:
                _LOGGER.info("There are no logs for this service in the last %s seconds", time_span)
                return None, None

        return data, raw

    def train(self):
        """Train models for anomaly detection."""
        start = time.time()

        data, _ = self._load_data(self.config.TRAIN_TIME_SPAN, self.config.TRAIN_MAX_ENTRIES)  # Get data for training
        if data is None:
            return 1

        _LOGGER.info("Learning Word2Vec Models and Saving for Inference Step")

        then = time.time()

        if not self.recreate_models and self.update_w2v_model:
            new_D, _ = self.w2v_model.update(data)
        else:
            new_D, _ = self.w2v_model.create(data)

        try:
            self.w2v_model.save(self.config.W2V_MODEL_PATH)
        except ModelSaveException as ex:
            _LOGGER.error("Failed to save W2V model: %s" % ex)
            raise

        now = time.time()

        _LOGGER.info("Training and Saving took %s minutes", ((now-then)/60))
        _LOGGER.info("Encoding Text Data")

        to_put_train = self.w2v_model.one_vector(new_D)

        _LOGGER.info("Start Training SOM...")

        then = time.time()

        if self.recreate_models or self.update_model:
                self.model.set(np.random.rand(24, 24, to_put_train.shape[1]))

        self.model.train(to_put_train, 24, self.config.TRAIN_ITERATIONS)
        now = time.time()

        _LOGGER.info("Training took %s minutes", ((now-then)/60))
        _LOGGER.info('Saving U-map')
        self.model.save_visualisation(self.config.MODEL_DIR)
        _LOGGER.info("Generating Baseline Metrics")

        dist = []
        cnt = 0
        total = len(to_put_train)
        for i in to_put_train:
            if not cnt % int(total/10):
                _LOGGER.info("Anomaly scoring %d/%d" % (cnt, total))
            dist.append(self.model.get_anomaly_score(i))
            cnt += 1

        self.model.set_metadata((np.mean(dist), np.std(dist), np.max(dist), np.min(dist)))
        try:
            self.model.save(self.config.MODEL_PATH)
        except ModelSaveException as ex:
            _LOGGER.error("Failed to save SOM model: %s" % ex)
            raise

        end = time.time()
        _LOGGER.info("Whole Process takes %s minutes", ((end-start)/60))

        return 0

    def infer(self):
        """Perform inference on trained models."""
        meta_data = self.model.get_metadata()
        maxx = meta_data[2]
        stdd = meta_data[1]

        _LOGGER.info("Maxx: %f, stdd: %f" % (maxx, stdd))

        _LOGGER.info("Models loaded, running %d infer loops" % self.config.INFER_LOOPS)

        infer_loops = 0
        while infer_loops < self.config.INFER_LOOPS:
            then = time.time()
            now = datetime.datetime.now()

            # Get data for inference
            data, json_logs = self._load_data(self.config.INFER_TIME_SPAN, self.config.INFER_MAX_ENTRIES)
            if data is None:
                time.sleep(5)
                continue

            _LOGGER.info("%d logs loaded from the last %d seconds", len(data) , self.config.INFER_TIME_SPAN)

            try:
                new_D, _ = self.w2v_model.update(data)
            except KeyError:
                _LOGGER.error("Word2Vec model fields incompatible with current log set. Retrain model with log data from the same service")
                exit()

            v = self.w2v_model.one_vector(new_D)

            dist = []
            for i in v:
                dist.append(self.model.get_anomaly_score(i))

            f = []

            _LOGGER.info("Max anomaly score: %f" % max(dist))
            for i in range(len(data)):
                _LOGGER.debug("Updating entry %d - dist: %f; maxx: %f" % (i, dist[i], maxx))
                s = json_logs[i]    # This needs to be more general, only works for ES incoming logs right now.
                s['anomaly_score'] = dist[i]

                if dist[i] > (self.config.INFER_ANOMALY_THRESHOLD * maxx):
                    s['anomaly'] = 1
                    _LOGGER.warn("Anomaly found (score: %f): %s" % (dist[i], s['message']))
                else:
                    s['anomaly'] = 0

                # _LOGGER.info("Storing entry (score: %f, anomaly: %d) with message: %s" % (s['anomaly_score'], s['anomaly'], s['message']))

                f.append(s)

            self.storage.store_results(f)

            # Inference done, increase counter
            infer_loops += 1

            now = time.time()
            _LOGGER.info("Analyzed one minute of data in %s seconds", (now-then))
            _LOGGER.info("waiting for next minute to start...")
            _LOGGER.info("press ctrl+c to stop process")
            sleep_time = self.config.INFER_TIME_SPAN-(now-then)
            if sleep_time > 0:
                time.sleep(sleep_time)

        # When we reached # of inference loops, retrain models
        self.recreate_models = True

    def run(self):
        """Run the main loop."""
        while True:
            if self.update_model or self.update_w2v_model or self.recreate_models:
                try:
                    self.train()
                except Exception as ex:
                    _LOGGER.error("Training failed: %s" % ex)
                    raise
            else:
                _LOGGER.info("Models already exists, skipping training")

            try:
                self.infer()
            except Exception as ex:
                _LOGGER.error("Inference failed: %s" % ex)
                time.sleep(5)