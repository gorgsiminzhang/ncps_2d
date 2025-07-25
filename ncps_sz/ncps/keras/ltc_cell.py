# Copyright 2022 Mathias Lechner and Ramin Hasani
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import keras
import numpy as np


@keras.utils.register_keras_serializable(package="ncps", name="LTCCell")
class LTCCell(keras.layers.Layer):
    name = "LTC-Cell"

    def __init__(
            self,
            wiring,
            input_mapping="affine",
            output_mapping="affine",
            ode_unfolds=6,
            epsilon=1e-8,
            initialization_ranges=None,
            **kwargs
    ):
        """A `Liquid time-constant (LTC) <https://ojs.aaai.org/index.php/AAAI/article/view/16936>`_ cell.

        . Note::
            This is an RNNCell that process single time-steps.
            To get a full RNN that can process sequences,
            see `ncps.keras.LTC` or wrap the cell with a `keras.layers.RNN <https://www.tensorflow.org/api_docs/python/tf/keras/layers/RNN>`_.

        Examples::

             >>> import ncps
             >>> from ncps.keras import LTCCell
             >>>
             >>> wiring = ncps.wirings.Random(16, output_dim=2, sparsity_level=0.5)
             >>> cell = LTCCell(wiring)
             >>> rnn = keras.layers.RNN(cell)
             >>> x = keras.random.uniform((1,4)) # (batch, features)
             >>> h0 = keras.ops.zeros((1, 16))
             >>> y = keras.layers.SimpleRNNCell(x,h0)
             >>>
             >>> x_seq = keras.random.uniform((1,20,4)) # (batch, time, features)
             >>> y_seq = rnn(x_seq)

        :param wiring:
        :param input_mapping:
        :param output_mapping:
        :param ode_unfolds:
        :param epsilon:
        :param initialization_ranges:
        :param kwargs:
        """

        super().__init__(**kwargs)
        self._init_ranges = {
            "gleak": (0.001, 1.0),
            "vleak": (-0.2, 0.2),
            "cm": (0.4, 0.6),
            "w": (0.001, 1.0),
            "sigma": (3, 8),
            "mu": (0.3, 0.8),
            "sensory_w": (0.001, 1.0),
            "sensory_sigma": (3, 8),
            "sensory_mu": (0.3, 0.8),
        }
        if initialization_ranges is not None:
            for k, v in initialization_ranges.items():
                if k not in self._init_ranges.keys():
                    raise ValueError(
                        "Unknown parameter '{}' in initialization range dictionary! (Expected only {})".format(
                            k, str(list(self._init_ranges.keys()))
                        )
                    )
                if k in ["gleak", "cm", "w", "sensory_w"] and v[0] < 0:
                    raise ValueError(
                        "Initialization range of parameter '{}' must be non-negative!".format(
                            k
                        )
                    )
                if v[0] > v[1]:
                    raise ValueError(
                        "Initialization range of parameter '{}' is not a valid range".format(
                            k
                        )
                    )
                self._init_ranges[k] = v

        self.wiring = wiring
        self._input_mapping = input_mapping
        self._output_mapping = output_mapping
        self._ode_unfolds = ode_unfolds
        self._epsilon = epsilon

    @property
    def state_size(self):
        return self.wiring.units

    @property
    def sensory_size(self):
        return self.wiring.input_dim

    @property
    def motor_size(self):
        return self.wiring.output_dim

    @property
    def output_size(self):
        return self.motor_size

    def _get_initializer(self, param_name):
        minval, maxval = self._init_ranges[param_name]
        if minval == maxval:
            return keras.initializers.Constant(minval)
        else:
            return keras.initializers.RandomUniform(minval, maxval)

    def build(self, input_shape):

        # Check if input_shape is nested tuple/list
        if isinstance(input_shape[0], tuple) or isinstance(input_shape[0], keras.KerasTensor):
            # Nested tuple -> First item represent feature dimension
            input_dim = input_shape[0][-1]
        else:
            input_dim = input_shape[-1]

        self.wiring.build(input_dim)

        self._params = {}
        self._params["gleak"] = self.add_weight(
            name="gleak",
            shape=(self.state_size,),
            dtype="float32",
            constraint=keras.constraints.NonNeg(),
            initializer=self._get_initializer("gleak"),
        )
        self._params["vleak"] = self.add_weight(
            name="vleak",
            shape=(self.state_size,),
            dtype="float32",
            initializer=self._get_initializer("vleak"),
        )
        self._params["cm"] = self.add_weight(
            name="cm",
            shape=(self.state_size,),
            dtype="float32",
            constraint=keras.constraints.NonNeg(),
            initializer=self._get_initializer("cm"),
        )
        self._params["sigma"] = self.add_weight(
            name="sigma",
            shape=(self.state_size, self.state_size),
            dtype="float32",
            initializer=self._get_initializer("sigma"),
        )
        self._params["mu"] = self.add_weight(
            name="mu",
            shape=(self.state_size, self.state_size),
            dtype="float32",
            initializer=self._get_initializer("mu"),
        )
        self._params["w"] = self.add_weight(
            name="w",
            shape=(self.state_size, self.state_size),
            dtype="float32",
            constraint=keras.constraints.NonNeg(),
            initializer=self._get_initializer("w"),
        )
        self._params["erev"] = self.add_weight(
            name="erev",
            shape=(self.state_size, self.state_size),
            dtype="float32",
            initializer=self.wiring.erev_initializer,
        )

        self._params["sensory_sigma"] = self.add_weight(
            name="sensory_sigma",
            shape=(self.sensory_size, self.state_size),
            dtype="float32",
            initializer=self._get_initializer("sensory_sigma"),
        )
        self._params["sensory_mu"] = self.add_weight(
            name="sensory_mu",
            shape=(self.sensory_size, self.state_size),
            dtype="float32",
            initializer=self._get_initializer("sensory_mu"),
        )
        self._params["sensory_w"] = self.add_weight(
            name="sensory_w",
            shape=(self.sensory_size, self.state_size),
            dtype="float32",
            constraint=keras.constraints.NonNeg(),
            initializer=self._get_initializer("sensory_w"),
        )
        self._params["sensory_erev"] = self.add_weight(
            name="sensory_erev",
            shape=(self.sensory_size, self.state_size),
            dtype="float32",
            initializer=self.wiring.sensory_erev_initializer,
        )

        self._params["sparsity_mask"] = keras.ops.convert_to_tensor(
            np.abs(self.wiring.adjacency_matrix), dtype="float32"
        )
        self._params["sensory_sparsity_mask"] = keras.ops.convert_to_tensor(
            np.abs(self.wiring.sensory_adjacency_matrix), dtype="float32"
        )

        if self._input_mapping in ["affine", "linear"]:
            self._params["input_w"] = self.add_weight(
                name="input_w",
                shape=(self.sensory_size,),
                dtype="float32",
                initializer=keras.initializers.Constant(1),
            )
        if self._input_mapping == "affine":
            self._params["input_b"] = self.add_weight(
                name="input_b",
                shape=(self.sensory_size,),
                dtype="float32",
                initializer=keras.initializers.Constant(0),
            )

        if self._output_mapping in ["affine", "linear"]:
            self._params["output_w"] = self.add_weight(
                name="output_w",
                shape=(self.motor_size,),
                dtype="float32",
                initializer=keras.initializers.Constant(1),
            )
        if self._output_mapping == "affine":
            self._params["output_b"] = self.add_weight(
                name="output_b",
                shape=(self.motor_size,),
                dtype="float32",
                initializer=keras.initializers.Constant(0),
            )
        self.built = True

    def _sigmoid(self, v_pre, mu, sigma):
        v_pre = keras.ops.expand_dims(v_pre, axis=-1)  # For broadcasting
        mues = v_pre - mu
        x = sigma * mues
        return keras.activations.sigmoid(x)

    def _ode_solver(self, inputs, state, elapsed_time):
        v_pre = state

        # We can pre-compute the effects of the sensory neurons here
        sensory_w_activation = self._params["sensory_w"] * self._sigmoid(
            inputs, self._params["sensory_mu"], self._params["sensory_sigma"]
        )
        sensory_w_activation *= self._params["sensory_sparsity_mask"]

        sensory_rev_activation = sensory_w_activation * self._params["sensory_erev"]

        # Reduce over dimension 1 (=source sensory neurons)
        w_numerator_sensory = keras.ops.sum(sensory_rev_activation, axis=1)
        w_denominator_sensory = keras.ops.sum(sensory_w_activation, axis=1)

        # cm/t is loop invariant
        cm_t = self._params["cm"] / keras.ops.cast(
            elapsed_time / self._ode_unfolds, dtype="float32"
        )

        # Unfold the multiply ODE multiple times into one RNN step
        for t in range(self._ode_unfolds):
            w_activation = self._params["w"] * self._sigmoid(
                v_pre, self._params["mu"], self._params["sigma"]
            )

            w_activation *= self._params["sparsity_mask"]

            rev_activation = w_activation * self._params["erev"]

            # Reduce over dimension 1 (=source neurons)
            w_numerator = keras.ops.sum(rev_activation, axis=1) + w_numerator_sensory
            w_denominator = keras.ops.sum(w_activation, axis=1) + w_denominator_sensory

            numerator = (
                    cm_t * v_pre
                    + self._params["gleak"] * self._params["vleak"]
                    + w_numerator
            )
            denominator = cm_t + self._params["gleak"] + w_denominator

            # Avoid dividing by 0
            v_pre = numerator / (denominator + self._epsilon)

        return v_pre

    def _map_inputs(self, inputs):
        if self._input_mapping in ["affine", "linear"]:
            inputs = inputs * self._params["input_w"]
        if self._input_mapping == "affine":
            inputs = inputs + self._params["input_b"]
        return inputs

    def _map_outputs(self, state):
        output = state
        if self.motor_size < self.state_size:
            output = output[:, 0: self.motor_size]

        if self._output_mapping in ["affine", "linear"]:
            output = output * self._params["output_w"]
        if self._output_mapping == "affine":
            output = output + self._params["output_b"]
        return output

    def call(self, sequence, states, training=False):
        if isinstance(sequence, (tuple, list)):
            # Irregularly sampled mode
            inputs, elapsed_time = sequence
        else:
            # Regularly sampled mode (elapsed time = 1 second)
            elapsed_time = 1.0
        inputs = self._map_inputs(sequence)

        next_state = self._ode_solver(inputs, states[0], elapsed_time)

        outputs = self._map_outputs(next_state)

        return outputs, [next_state]

    def get_config(self):
        config = super(LTCCell, self).get_config()
        config["wiring"] = self.wiring.get_config()
        config["input_mapping"] = self._input_mapping
        config["output_mapping"] = self._output_mapping
        config["ode_unfolds"] = self._ode_unfolds
        config["epsilon"] = self._epsilon
        return config

    @classmethod
    def from_config(cls, config):
        return cls(**config)
