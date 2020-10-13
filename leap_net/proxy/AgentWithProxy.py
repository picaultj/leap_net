# Copyright (c) 2019-2020, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of leap_net, leap_net a keras implementation of the LEAP Net model.
import os
import json
import re

import numpy as np
import tensorflow as tf

from collections.abc import Iterable
from grid2op.Agent import BaseAgent

from leap_net.proxy.ProxyLeapNet import ProxyLeapNet


class AgentWithProxy(BaseAgent):
    """
    Add to an agent a proxy leap net (usefull to train a leap net model)

    TODO add an example of usage


    """
    def __init__(self,
                 agent_action,  # the agent that will take some actions
                 logdir="tf_logs",
                 update_tensorboard=256,  # tensorboard is updated every XXX training iterations
                 save_freq=256,  # model is saved every save_freq training iterations
                 ext=".h5",  # extension of the file in which you want to save the proxy

                 name="leap_net",
                 max_row_training_set=int(1e5),
                 batch_size=32,
                 attr_x=("prod_p", "prod_v", "load_p", "load_q"),
                 attr_y=("a_or", "a_ex", "p_or", "p_ex", "q_or", "q_ex", "prod_q", "load_v"),
                 attr_tau=("line_status", ),
                 sizes_enc=(20, 20, 20),
                 sizes_main=(150, 150, 150),
                 sizes_out=(100, 40),
                 lr=1e-4,
                 ):
        BaseAgent.__init__(self, agent_action.action_space)
        self.agent_action = agent_action

        # to fill the training / test dataset
        self.max_row_training_set = max_row_training_set
        self.batch_size = batch_size
        self.global_iter = 0
        self.train_iter = 0
        self.__is_init = False  # is this model initiliazed
        self.is_training = True

        # proxy part
        self._proxy = ProxyLeapNet(name=name,
                                   max_row_training_set=max_row_training_set, batch_size=batch_size,
                                   attr_x=attr_x, attr_y=attr_y, attr_tau=attr_tau,
                                   sizes_enc=sizes_enc, sizes_main=sizes_main, sizes_out=sizes_out,
                                   lr=lr)

        # tensorboard (should be initialized after the proxy)
        if logdir is not None:
            logpath = os.path.join(logdir, self.get_name())
            self._tf_writer = tf.summary.create_file_writer(logpath, name=self.get_name())
        else:
            logpath = None
            self._tf_writer = None
        self.update_tensorboard = update_tensorboard
        self.save_freq = save_freq

        # save load
        if re.match(r"^\.", ext) is None:
            # add a point at the beginning of the extension
            self.ext = f".{ext}"
        else:
            self.ext = ext
        self.save_path = None

    def init(self, obs):
        """

        Parameters
        ----------
        obs

        Returns
        -------

        """
        self.__is_init = True

        # now build the poxy
        self._proxy.init(obs)
        self._proxy.build_model()

    # agent interface
    def act(self, obs, reward, done=False):
        self.store_obs(obs)
        if self.is_training:
            batch_losses = self._proxy.train(tf_writer=self._tf_writer)
            if batch_losses is not None:
                self.train_iter += 1
                self._save_tensorboard(batch_losses)
                self._save_model()
        return self.agent_action.act(obs, reward, done)

    def store_obs(self, obs):
        """
        store the observation into the "database" for training the model.

        Notes
        -------
        Will also increment `self.last_id`

        Parameters
        ----------
        obs: `grid2op.Action.BaseObservation`
            The current observation
        """
        if not self.__is_init:
            self.init(obs)

        self._proxy.store_obs(obs)

    def train(self, env, total_training_step, save_path=None, load_path=None):
        """
        Completely train the proxy

        Parameters
        ----------
        env
        total_training_step

        Returns
        -------

        """
        obs = self._reboot(env)
        done = False
        reward = env.reward_range[0]
        self.save_path = save_path
        if self.save_path is not None:
            if not os.path.exists(self.save_path):
                os.mkdir(self.save_path)
        if load_path is not None:
            self.load(load_path)
        self.is_training = True
        with tqdm(total=total_training_step) as pbar:
            # update the progress bar
            pbar.update(self.global_iter)

            # and do the "gym loop"
            while not done:
                act = self.act(obs, reward, done)
                # TODO handle multienv here
                obs, reward, done, info = env.step(act)
                if done:
                    obs = self._reboot(env)
                    done = False
                self.global_iter += 1
                pbar.update(1)
                if self.global_iter >= total_training_step:
                    break
        # save the model at the end
        self.save(self.save_path)

    def evaluate(self, env, total_evaluation_step, load_path, save_path=None, metrics=None):
        """

        Parameters
        ----------
        env
        total_evaluation_step
        load_path
        save_path
        metrics:
            dictionary of function, with keys being the metrics name, and values the function that compute
            this metric (on the whole output) that should be `metric_fun(y_true, y_pred)`

        Returns
        -------

        """
        obs = self._reboot(env)
        done = False
        reward = env.reward_range[0]
        self.is_training = False

        if load_path is not None:
            self.load(load_path)
            self.global_iter = 0
            self.save_path = None  # disable the saving of the model

        # TODO find a better approach for more general proxy that can adapt to grid of different size
        sizes = self._proxy.get_output_sizes()
        true_val = [np.zeros((total_evaluation_step, el), dtype=self._proxy.dtype) for el in sizes]
        pred_val = [np.zeros((total_evaluation_step, el), dtype=self._proxy.dtype) for el in sizes]

        with tqdm(total=total_evaluation_step) as pbar:
            # update the progress bar
            pbar.update(self.global_iter)

            # and do the "gym loop"
            while not done:
                act = self.act(obs, reward, done)

                # save the predictions and the reference
                predictions = self._proxy.predict()
                for arr_, pred_ in zip(pred_val, predictions):
                    arr_[self.global_iter, :] = pred_.reshape(-1)
                reality = self._proxy.get_true_output(obs)
                for arr_, ref_ in zip(true_val, reality):
                    arr_[self.global_iter, :] = ref_.reshape(-1)

                # TODO handle multienv here (this might be more complicated!)
                obs, reward, done, info = env.step(act)
                if done:
                    obs = self._reboot(env)
                    done = False
                self.global_iter += 1
                pbar.update(1)
                if self.global_iter >= total_evaluation_step:
                    break

        # save the results and compute the metrics
        self._save_results(obs, save_path, metrics, pred_val, true_val)
        # TODO save the x's too!

    def save(self, path):
        """
        Part of the l2rpn_baselines interface, this allows to save a model. Its name is used at saving time. The
        same name must be reused when loading it back.

        Parameters
        ----------
        path: ``str``
            The path where to save the agent.

        """
        if path is not None:
            path_save = os.path.join(path, self.get_name())
            if not os.path.exists(path_save):
                os.mkdir(path_save)
            self._save_metadata(path_save)
            self._proxy.save_weights(path=path_save, ext=self.ext)

    def load(self, path):
        if path is not None:
            # the following if is to be able to restore a file with possibly a different name...
            if self.is_training:
                path_model = self._get_path_nn(path, self.get_name())
            else:
                path_model = path
            if not os.path.exists(path_model):
                raise RuntimeError(f"You asked to load a model at \"{path_model}\" but there is nothing there.")
            self._load_metadata(path_model)
            self._proxy.build_model()
            self._proxy.load_weights(path=path, ext=self.ext)

    def get_name(self):
        return self._proxy.name

    # save load model
    def _get_path_nn(self, path, name):
        if name is None:
            path_model = path
        else:
            path_model = os.path.join(path, name)
        return path_model

    def _save_metadata(self, path_model):
        """save the dimensions of the models and the scalers"""
        json_nm = "metadata.json"
        me = self._to_dict()
        with open(os.path.join(path_model, json_nm), "w", encoding="utf-8") as f:
            json.dump(obj=me, fp=f)

    def _load_metadata(self, path_model):
        json_nm = "metadata.json"
        with open(os.path.join(path_model, json_nm), "r", encoding="utf-8") as f:
            me = json.load(f)
        self._from_dict(me)

    def _to_dict(self):
        res = {}
        res["proxy"] = self._proxy.get_metadata()
        res["train_iter"] = int(self.train_iter)
        res["global_iter"] = int(self.global_iter)
        return res

    def _save_dict(self, li, val):
        if isinstance(val, Iterable):
            li.append([float(el) for el in val])
        else:
            li.append(float(val))

    def _from_dict(self, dict_):
        """modify self! """
        self.train_iter = int(dict_["train_iter"])
        self.global_iter = int(dict_["global_iter"])
        self._proxy.load_metadata(dict_["proxy"])

    def _save_tensorboard(self, batch_losses):
        """save all the information needed in tensorboard."""
        if self._tf_writer is None:
            return

        # Log some useful metrics every even updates
        if self.train_iter % self.update_tensorboard == 0:
            with self._tf_writer.as_default():
                # save total loss
                tf.summary.scalar(f"global loss",
                                  np.sum(batch_losses),
                                  self.train_iter,
                                  description="loss for the entire model")
                self._proxy.save_tensorboard(self._tf_writer, self.train_iter, batch_losses)

    def _save_model(self):
        if self.train_iter % self.save_freq == 0:
            self.save(self.save_path)

    def _save_results(self, obs, save_path, metrics, pred_val, true_val):

        # compute the metrics (if any)
        dict_metrics = {}
        dict_metrics["predict_step"] = int(self.global_iter)
        dict_metrics["predict_time"] = float(self._proxy.get_total_predict_time())
        dict_metrics["avg_pred_time_s"] = float(self._proxy.get_total_predict_time()) / float(self.global_iter)
        if metrics is not None:
            array_names = self._proxy.get_attr_output_name(obs)
            for metric_name, metric_fun in metrics.items():
                dict_metrics[metric_name] = {}
                for nm, pred_, true_ in zip(array_names, pred_val, true_val):
                    tmp = metric_fun(true_, pred_)
                    # print the results and make sure the things are json serializable
                    if isinstance(tmp, Iterable):
                        print(f"{metric_name} for {nm}: {tmp}")
                        dict_metrics[metric_name][nm] = [float(el) for el in tmp]
                    else:
                        print(f"{metric_name} for {nm}: {tmp:.2f}")
                        dict_metrics[metric_name][nm] = float(tmp)

        # save the numpy arrays (if needed)
        if save_path is not None:
            # save the proxy and the meta data
            self.save(save_path)

            # now the other data
            array_names = self._proxy.get_attr_output_name(obs)
            save_path = os.path.join(save_path, self.get_name())
            if not os.path.exists(save_path):
                os.mkdir(save_path)

            for nm, pred_, true_ in zip(array_names, pred_val, true_val):
                np.save(os.path.join(save_path, f"{nm}_pred.npy"), pred_)
                np.save(os.path.join(save_path, f"{nm}_real.npy"), true_)
            with open(os.path.join(save_path, "metrics.json"), "w", encoding="utf-8") as f:
                json.dump(dict_metrics, fp=f, indent=4, sort_keys=True)
        return dict_metrics

    def _reboot(self, env):
        """when an environment is "done" this function reset it and act a first time with the agent_action"""
        # TODO skip and random start at some steps
        done = False
        reward = env.reward_range[0]
        obs = env.reset()
        obs, reward, done, info = env.step(self.agent_action.act(obs, reward, done))
        while done:
            # we restart until we find an environment that is not "game over"
            obs = env.reset()
            obs, reward, done, info = env.step(self.agent_action.act(obs, reward, done))
        return obs


if __name__ == "__main__":
    import grid2op
    from grid2op.Parameters import Parameters
    from leap_net.generate_data.Agents import RandomN1, RandomNN1, RandomN2
    from tqdm import tqdm
    from lightsim2grid.LightSimBackend import LightSimBackend
    from sklearn.metrics import mean_squared_error, mean_absolute_error  #, mean_absolute_percentage_error

    total_train = 11*12*int(2e5)
    total_train = 12*int(1e5)
    total_evaluation_step = int(1e4)
    env_name = "l2rpn_case14_sandbox"
    model_name = "test_refacto2"
    save_path = "model_saved"
    save_path_final_results = "model_results"

    # generate the environment
    param = Parameters()
    param.NO_OVERFLOW_DISCONNECTION = True
    param.NB_TIMESTEP_COOLDOWN_LINE = 0
    param.NB_TIMESTEP_COOLDOWN_SUB = 0
    env = grid2op.make(param=param, backend=LightSimBackend())
    agent = RandomNN1(env.action_space, p=0.5)
    agent_with_proxy = AgentWithProxy(agent,
                                      name=model_name,
                                      max_row_training_set=int(total_train/10))
    # train it
    agent_with_proxy.train(env,
                           total_train,
                           save_path=save_path
                           )
    # evaluate this agent
    agent_with_proxy.evaluate(env,
                              total_evaluation_step=total_evaluation_step,
                              load_path=os.path.join(save_path, model_name),
                              save_path=save_path_final_results,
                              metrics={"MSE": lambda y_true, y_pred: mean_squared_error(y_true, y_pred, multioutput="raw_values"),
                                       "MAE": lambda y_true, y_pred: mean_absolute_error(y_true, y_pred, multioutput="raw_values"),
                                       # "MAPE": mean_absolute_percentage_error
                                       })
    # now evaluate the agent when another "agent" is used (different data distribution)
    agent_eval = RandomN2(env.action_space)
    agent_with_proxy_eval = AgentWithProxy(agent_eval,
                                           name=f"{model_name}_evalN2",
                                           max_row_training_set=total_evaluation_step)
    agent_with_proxy_eval.evaluate(env,
                                   total_evaluation_step=total_evaluation_step,
                                   load_path=os.path.join(save_path, model_name),
                                   save_path=save_path_final_results,
                                   metrics={"MSE": lambda y_true, y_pred: mean_squared_error(y_true, y_pred, multioutput="raw_values"),
                                            "MAE": lambda y_true, y_pred: mean_absolute_error(y_true, y_pred, multioutput="raw_values"),
                                            # "MAPE": mean_absolute_percentage_error
                                            }
                                   )
