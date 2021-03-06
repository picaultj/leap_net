# Copyright (c) 2019-2020, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of leap_net, leap_net a keras implementation of the LEAP Net model.

import copy
import warnings
import numpy as np

import tensorflow as tf
from tensorflow.keras.layers import Dense
from tensorflow.keras.layers import multiply as tfk_multiply

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=FutureWarning)
    from tensorflow.keras.models import Sequential, Model
    from tensorflow.keras.layers import Activation
    from tensorflow.keras.layers import Input

from leap_net.proxy.BaseNNProxy import BaseNNProxy
from leap_net.LtauNoAdd import LtauNoAdd


class ProxyLeapNet(BaseNNProxy):
    """
    This class demonstrate how to implement a proxy based on a neural network with the leap net architecture.

    This proxy is fully functional and some examples of training / evaluation can be found in the scripts
    `train_proxy_case_14.py`, `train_proxy_case_118.py`, `evaluate_proxy_case_14.py` and
    `evaluate_proxy_case_118.py`.

    It scales the data and has 3 different datasets:

    - `_my_x` : present in the base class :attr:`BaseNNProxy._my_x` representing the regular input to the neural
      network
    - `_my_y` : present in the base class :attr:`BaseNNProxy._my_y` representing what the neural network need
      to predict
    - `_my_tau`: representing the "tau" vectors.

    So this class also demonstrates how the generic interface can be adapted in case you want to deal with different
    data scheme (in this case 2 inputs and 1 outputs)

    """
    def __init__(self,
                 name="leap_net",
                 max_row_training_set=int(1e5),
                 train_batch_size=32,
                 eval_batch_size=1024,
                 attr_x=("prod_p", "prod_v", "load_p", "load_q"),
                 attr_y=("a_or", "a_ex", "p_or", "p_ex", "q_or", "q_ex", "prod_q", "load_v", "v_or", "v_ex"),
                 attr_tau=("line_status",),
                 sizes_enc=(20, 20, 20),
                 sizes_main=(150, 150, 150),
                 sizes_out=(100, 40),
                 lr=1e-4,
                 scale_main_layer=None,  # increase the size of the main layer
                 scale_input_dec_layer=None,  # scale the input of the decoder
                 scale_input_enc_layer=None,  # scale the input of the encoder
                 layer=Dense,  # TODO (for save and restore)
                 layer_act=None
                 ):
        BaseNNProxy.__init__(self,
                             name=name,
                             lr=lr,
                             max_row_training_set=max_row_training_set,
                             train_batch_size=train_batch_size,
                             eval_batch_size=eval_batch_size,
                             attr_x=attr_x,
                             attr_y=attr_y,
                             layer=layer,
                             layer_act=layer_act)
        # datasets
        self._my_tau = None
        self._sz_tau = None

        # scalers
        self._m_x = None  # TODO move that into the baseNN class
        self._m_y = None  # TODO move that into the baseNN class
        self._m_tau = None
        self._sd_x = None  # TODO move that into the baseNN class
        self._sd_y = None  # TODO move that into the baseNN class
        self._sd_tau = None

        # specific part to leap net model
        # TODO to make sure it's integers
        self.sizes_enc = sizes_enc
        self.sizes_main = sizes_main
        self.sizes_out = sizes_out
        self.attr_tau = attr_tau
        self._scale_main_layer = scale_main_layer
        self._scale_input_dec_layer = scale_input_dec_layer
        self._scale_input_enc_layer = scale_input_enc_layer

        # not to load multiple times the meta data

        # small stuff with powerlines (force prediction of 0 when powerline is disconnected)
        # attributes that are stored as lines
        self._line_attr = {"a_or", "a_ex", "p_or", "p_ex", "q_or", "q_ex", "v_or", "v_ex"}
        self.tensor_line_status = None
        self._idx = None
        self._where_id = None
        self.tensor_line_status = None
        try:
            self._idx = self.attr_tau.index("line_status")
            self._where_id = "tau"
        except ValueError:
            try:
                self._idx = self.attr_x.index("line_status")
                self._where_id = "x"
            except ValueError:
                warnings.warn("We strongly recommend you to get the \"line_status\" as an input vector")

    def build_model(self):
        """build the neural network used as proxy, in this case a leap net."""
        if self._model is not None:
            # model is already initialized
            return
        self._model = Sequential()
        inputs_x = [Input(shape=(el,), name="x_{}".format(nm_)) for el, nm_ in
                    zip(self._sz_x, self.attr_x)]
        inputs_tau = [Input(shape=(el,), name="tau_{}".format(nm_)) for el, nm_ in
                      zip(self._sz_tau, self.attr_tau)]

        # tensor_line_status = None
        if self._idx is not None:
            # line status is encoded: 1 disconnected, 0 connected
            # I invert it here
            if self._where_id == "x":
                self.tensor_line_status = inputs_x[self._idx]
            elif self._where_id == "tau":
                self.tensor_line_status = inputs_tau[self._idx]
            else:
                raise RuntimeError("Unknown \"where_id\"")
            self.tensor_line_status = 1.0 - self.tensor_line_status

        # encode each data type in initial layers
        encs_out = []
        for init_val, nm_ in zip(inputs_x, self.attr_x):
            lay = init_val

            if self._scale_input_enc_layer is not None:
                # scale up to have higher dimension
                lay = Dense(self._scale_input_enc_layer,
                            name=f"scaling_input_encoder_{nm_}")(lay)
            for i, size in enumerate(self.sizes_enc):
                lay_fun = self._layer_fun(size,
                                          name="enc_{}_{}".format(nm_, i),
                                          activation=self._layer_act)
                lay = lay_fun(lay)
                if self._layer_act is None:
                    # add a non linearity if not added in the layer
                    lay = Activation("relu")(lay)
            encs_out.append(lay)

        # concatenate all that
        lay = tf.keras.layers.concatenate(encs_out)

        if self._scale_main_layer is not None:
            # scale up to have higher dimension
            lay = Dense(self._scale_main_layer, name="scaling_inputs")(lay)

        # i do a few layer
        for i, size in enumerate(self.sizes_main):
            lay_fun = self._layer_fun(size,
                                      name="main_{}".format(i),
                                      activation=self._layer_act)
            lay = lay_fun(lay)
            if self._layer_act is None:
                # add a non linearity if not added in the layer
                lay = Activation("relu")(lay)

        # now i do the leap net to encode the state
        encoded_state = lay
        for input_tau, nm_ in zip(inputs_tau, self.attr_tau):
            tmp = LtauNoAdd(name=f"leap_{nm_}")([lay, input_tau])
            encoded_state = tf.keras.layers.add([encoded_state, tmp], name=f"adding_{nm_}")

        # i predict the full state of the grid given the input variables
        outputs_gm = []
        model_losses = {}
        # model_losses = []
        lossWeights = {}  # TODO
        for sz_out, nm_ in zip(self._sz_y,
                               self.attr_y):
            lay = encoded_state
            if self._scale_input_dec_layer is not None:
                # scale up to have higher dimension
                lay = Dense(self._scale_input_dec_layer,
                            name=f"scaling_input_decoder_{nm_}")(lay)
                lay = Activation("relu")(lay)

            for i, size in enumerate(self.sizes_out):
                lay_fun = self._layer_fun(size,
                                          name="{}_{}".format(nm_, i),
                                          activation=self._layer_act)
                lay = lay_fun(lay)
                if self._layer_act is None:
                    # add a non linearity if not added in the layer
                    lay = Activation("relu")(lay)

            # predict now the variable
            name_output = "{}_hat".format(nm_)
            # force the model to output 0 when the powerline is disconnected
            if self.tensor_line_status is not None and nm_ in self._line_attr:
                pred_ = Dense(sz_out, name=f"{nm_}_force_disco")(lay)
                pred_ = tfk_multiply((pred_, self.tensor_line_status), name=name_output)
            else:
                pred_ = Dense(sz_out, name=name_output)(lay)

            outputs_gm.append(pred_)
            model_losses[name_output] = "mse"
            # model_losses.append(tf.keras.losses.mean_squared_error)

        # now create the model in keras
        self._model = Model(inputs=(inputs_x, inputs_tau),
                            outputs=outputs_gm,
                            name="model")
        # and "compile" it
        self._schedule_lr_model, self._optimizer_model = self._make_optimiser()
        self._model.compile(loss=model_losses, optimizer=self._optimizer_model)

    def store_obs(self, obs):
        """
        store the observation into the "training database"

        This would not be necessary to overide it in "regular" model, but in this case we also need to store
        the "tau".

        The storing of X and Y is done automatically in the base class, hence the call of `super().store_obs(obs)`
        """
        # save the specific part to tau
        for attr_nm, inp in zip(self.attr_tau, self._my_tau):
            inp[self.last_id, :] = self._extract_obs(obs, attr_nm)

        # save the observation in the database
        super().store_obs(obs)

    def init(self, obss):
        """
        Initialize all the meta data and the database for training

        Parameters
        ----------
        obs

        Returns
        -------

        """

        if not self._metadata_loaded:
            # ini the vector tau
            self._sz_tau = []
            for attr_nm in self.attr_tau:
                arr_ = self._extract_obs(obss[0], attr_nm)
                sz = arr_.size
                self._sz_tau.append(sz)

        # init the rest (attributes of the base class)
        super().init(obss)

        # deals with normalization #TODO some of it might be done in the base class
        # initialize mean and standard deviation
        # but only if the model is being built, not if it has been reloaded
        if not self._metadata_loaded:
            # for the input
            self._m_x = []
            self._sd_x = []
            for attr_nm in self.attr_x:
                self._m_x.append(self._get_mean(obss, attr_nm))
                self._sd_x.append(self._get_sd(obss, attr_nm))

            # for the output
            self._m_y = []
            self._sd_y = []
            for attr_nm in self.attr_y:
                self._m_y.append(self._get_mean(obss, attr_nm))
                self._sd_y.append(self._get_sd(obss, attr_nm))

            # for the tau vectors
            self._m_tau = []
            self._sd_tau = []
            for attr_nm in self.attr_tau:
                self._m_tau.append(self._get_mean(obss, attr_nm))
                self._sd_tau.append(self._get_sd(obss, attr_nm))

        self._metadata_loaded = True

    def get_metadata(self):
        res = super().get_metadata()
        # save attribute for the "extra" database
        res["attr_tau"] = [str(el) for el in self.attr_tau]
        res["_sz_tau"] = [int(el) for el in self._sz_tau]

        # save means and standard deviation
        res["_m_x"] = []
        for el in self._m_x:
            self._save_dict(res["_m_x"], el)
        res["_m_y"] = []
        for el in self._m_y:
            self._save_dict(res["_m_y"], el)
        res["_m_tau"] = []
        for el in self._m_tau:
            self._save_dict(res["_m_tau"], el)
        res["_sd_x"] = []
        for el in self._sd_x:
            self._save_dict(res["_sd_x"], el)
        res["_sd_y"] = []
        for el in self._sd_y:
            self._save_dict(res["_sd_y"], el)
        res["_sd_tau"] = []
        for el in self._sd_tau:
            self._save_dict(res["_sd_tau"], el)

        # store the sizes
        res["sizes_enc"] = [int(el) for el in self.sizes_enc]
        res["sizes_main"] = [int(el) for el in self.sizes_main]
        res["sizes_out"] = [int(el) for el in self.sizes_out]

        # store some information about some transformations we can do
        if self._scale_main_layer is not None:
            res["_scale_main_layer"] = int(self._scale_main_layer)
        else:
            # i don't store anything if it's None
            pass
        if self._scale_input_dec_layer is not None:
            res["_scale_input_dec_layer"] = int(self._scale_input_dec_layer)
        else:
            # i don't store anything if it's None
            pass
        if self._scale_input_enc_layer is not None:
            res["_scale_input_enc_layer"] = int(self._scale_input_enc_layer)
        else:
            # i don't store anything if it's None
            pass
        return res

    def _init_database_shapes(self):
        """
        Again this method is only overriden because the leap net takes inputs in two different ways: the X's
        and the tau's
        """
        super()._init_database_shapes()
        self._my_tau = []
        for sz in self._sz_tau:
            self._my_tau.append(np.zeros((self.max_row_training_set, sz), dtype=self.dtype))

    def load_metadata(self, dict_):
        """
        load the metadata of this neural network (also called meta parameters) from a dictionary
        """
        self.attr_tau = tuple([str(el) for el in dict_["attr_tau"]])
        self._sz_tau = [int(el) for el in dict_["_sz_tau"]]
        super().load_metadata(dict_)

        for key in ["_m_x", "_m_y", "_m_tau", "_sd_x", "_sd_y", "_sd_tau"]:
            setattr(self, key, [])
            for el in dict_[key]:
                self._add_attr(key, el)

        self.sizes_enc = [int(el) for el in dict_["sizes_enc"]]
        self.sizes_main = [int(el) for el in dict_["sizes_main"]]
        self.sizes_out = [int(el) for el in dict_["sizes_out"]]
        if "_scale_main_layer" in dict_:
            self._scale_main_layer = int(dict_["_scale_main_layer"])
        else:
            self._scale_main_layer = None
        if "_scale_input_dec_layer" in dict_:
            self._scale_input_dec_layer = int(dict_["_scale_input_dec_layer"])
        else:
            self._scale_input_dec_layer = None
        if "_scale_input_enc_layer" in dict_:
            self._scale_input_enc_layer = int(dict_["_scale_input_enc_layer"])
        else:
            self._scale_input_enc_layer = None
        if "_layer_act" in dict_:
            self._layer_act = str(dict_["_layer_act"])
        else:
            self._layer_act = None

    def _extract_data(self, indx_train):
        """
        extract from the training dataset, the data with indexes `indx_train`

        The model will be trained with a code equivalent to:

        .. code-block:: python

            data = self._extract_data(indx_train)
            batch_losses = self._train_model(data)

        This function is also used for the evaluation of the model in the following manner:

        .. code-block:: python

            data = self._extract_data(indx_val)
            res = self._make_predictions(data, training=False)

        Here we needed to override it for two reasons:

        - we use 3 different data (X,tau, Y) this is specific to leap net
        - we wanted to scale the data passed to the neural networks

        Parameters
        ----------
        indx_train: ``numpy.ndarray``, ``int``
            The index of the data that needs to be retrieved from the database `_my_x` and `_my_y`

        Returns
        -------
        X:
            The value of the input data
        Y:
            The value of the desired output of the proxy

        """


        # tf.convert_to_tensor(
        tmpx = [(arr[indx_train, :] - m_) / sd_ for arr, m_, sd_ in zip(self._my_x, self._m_x, self._sd_x)]
        tmpt = [(arr[indx_train, :] - m_) / sd_ for arr, m_, sd_ in zip(self._my_tau, self._m_tau, self._sd_tau)]
        tmpy = [(arr[indx_train, :] - m_) / sd_ for arr, m_, sd_ in zip(self._my_y, self._m_y, self._sd_y)]

        # tmp_line_status = 1.0
        # TODO if i do it here, i need to do it also on the post process, and this is not great
        # if self._idx is not None:
        #     if self._where_id == "tau":
        #         tmp_line_status = tmpt[self._idx]
        #     elif self._where_id == "x":
        #         tmp_line_status = tmpx[self._idx]
        #     else:
        #         raise RuntimeError("Unknown self._where_id")
        # tmpy = [tf.convert_to_tensor((arr[indx_train, :] - m_) / sd_ * tmp_line_status if attr_n in self.line_attr else 1.0)
        #         for arr, m_, sd_, attr_n in zip(self._my_y, self._m_y, self._sd_y, self.attr_y)]

        tmpx = [tf.convert_to_tensor(el) for el in tmpx]
        tmpt = [tf.convert_to_tensor(el) for el in tmpt]
        tmpy = [tf.convert_to_tensor(el) for el in tmpy]
        return (tmpx, tmpt), tmpy

    def _post_process(self, predicted_state):
        """
        This function is used to post process the data that are the output of the proxy.

        In our case we needed to code it because we applied some scaling when the data were "extracted" from the
        internal database (we overide :func:`ProxyLeapNet._extract_data`)
        """
        tmp = [el.numpy() for el in predicted_state]
        resy = [arr * sd_ + m_ for arr, m_, sd_ in zip(tmp, self._m_y, self._sd_y)]
        return resy
