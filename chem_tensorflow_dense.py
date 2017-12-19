#!/usr/bin/env/python
"""
Usage:
    chem_tensorflow_dense.py [options]

Options:
    -h --help                Show this screen.
    --config-file FILE       Hyperparameter configuration file path (in JSON format)
    --config CONFIG          Hyperparameter configuration dictionary (in JSON format)
    --log_dir NAME           log dir name
    --data_dir NAME          data dir name
"""

from typing import Sequence, Any
from docopt import docopt
from collections import defaultdict
import numpy as np
import tensorflow as tf
import sys, traceback
import pdb

from chem_tensorflow import ChemModel
from utils import glorot_init


def graph_to_adj_mat(graph, max_n_vertices, num_edge_types, tie_fwd_bkwd=True):
    amat_width = max_n_vertices if tie_fwd_bkwd else 2*max_n_vertices
    amat = np.zeros((num_edge_types, max_n_vertices, amat_width))
    for src, e, dest in graph:
        amat[e-1, dest, src] = 1
        offset = 0 if tie_fwd_bkwd else 4
        amat[e + offset-1, src, dest] = 1
    return amat


class DenseGGNNChemModel(ChemModel):
    def __init__(self, args):
        super().__init__(args)

    @classmethod
    def default_params(cls):
        params = dict(super().default_params())
        params.update({'batch_size': 256})
        return params

    def prepare_specific_model(self) -> None:
        h_dim = self.params['hidden_size']
        # inputs
        self.placeholders['initial_node_representation'] = tf.placeholder(tf.float32,
                                                                          [None, None, self.params['hidden_size']],
                                                                          name='node_features')
        self.placeholders['num_vertices'] = tf.placeholder(tf.int32, ())
        self.placeholders['adjacency_matrix'] = tf.placeholder(tf.float32,
                                                               [None, self.num_edge_types, None, None])  # [b x e x v x v]
        self.__adjacency_matrix = tf.transpose(self.placeholders['adjacency_matrix'], [1, 0, 2, 3])  # [e x b x v x v]

        # weights
        self.weights['edge_weights'] = tf.Variable(glorot_init([self.num_edge_types, h_dim, h_dim]))
        self.weights['edge_biases'] = tf.Variable(np.zeros([self.num_edge_types, 1, h_dim]).astype(np.float32))
        with tf.variable_scope("gru_scope"):
            self.weights['node_gru'] = tf.contrib.rnn.GRUCell(h_dim)

    def compute_final_node_representations(self) -> tf.Tensor:
        v = self.placeholders['num_vertices']
        h_dim = self.params['hidden_size']
        h = self.placeholders['initial_node_representation']  # [b x v x h]
        h = tf.reshape(h, [-1, h_dim])

        biases = []
        for a in tf.unstack(self.__adjacency_matrix, axis=0):
            summed_a = tf.reshape(tf.reduce_sum(a, axis=-1), [-1, 1])  # [b*v x 1]
            biases.append(tf.matmul(summed_a, self.weights['edge_biases'][0]))  # [b*v x h]
        with tf.variable_scope("gru_scope") as scope:
            for i in range(self.params['num_timesteps']):
                if i > 0:
                    tf.get_variable_scope().reuse_variables()
                for edge_type in range(self.num_edge_types):
                    m = tf.matmul(h, self.weights['edge_weights'][edge_type]) + biases[edge_type]  # [b*v x h]
                    m = tf.reshape(m, [-1, v, h_dim])  # [b x v x h]
                    if edge_type == 0:
                        acts = tf.matmul(self.__adjacency_matrix[edge_type], m)
                    else:
                        acts += tf.matmul(self.__adjacency_matrix[edge_type], m)
                acts = tf.reshape(acts, [-1, h_dim])  # [b*v x h]
                h = self.weights['node_gru'](acts, h)[0]  # [b*v x h]
            last_h = tf.reshape(h, [-1, v, h_dim])
        return last_h

    def gated_regression(self, last_h):
        # last_h: [b x v x h]
        gate_input = tf.concat([last_h, self.placeholders['initial_node_representation']], axis = 2)     # [b x v x 2h]
        gate_input = tf.reshape(gate_input, [-1, 2 * self.params["hidden_size"]])                        # [(b*v) x 2h]
        last_h = tf.reshape(last_h, [-1, self.params["hidden_size"]])                                    # [(b*v) x h]
        gated_outputs = tf.nn.sigmoid(self.weights['regression_gate'](gate_input)) \
                                      * self.weights['regression_transform'](last_h)                     # [(b*v) x 1]
        gated_outputs = tf.reshape(gated_outputs, [-1, self.placeholders['num_vertices']])               # [b x v]
        output = tf.reduce_sum(gated_outputs, axis = 1)                                                  # [b]
        return output

    # ----- Data preprocessing and chunking into minibatches:
    def process_raw_graphs(self, raw_data: Sequence[Any]) -> Any:
        bucket_sizes = np.array(list(range(4, 28, 2)) + [29])
        bucketed = defaultdict(list)
        x_dim = len(raw_data[0]["node_features"][0])
        for d in raw_data:
            chosen_bucket_idx = np.argmax(bucket_sizes > max([v for e in d['graph']
                                                                for v in [e[0], e[2]]]))
            chosen_bucket_size = bucket_sizes[chosen_bucket_idx]
            bucketed[chosen_bucket_idx].append({
                'adj_mat': graph_to_adj_mat(d['graph'], chosen_bucket_size, self.num_edge_types, self.params['tie_fwd_bkwd']),
                'init': d["node_features"] + [[0 for _ in range(x_dim)] for __ in
                                              range(chosen_bucket_size - len(d["node_features"]))],
                'label': d["targets"][self.params['task_id']][0]
            })

        bucket_at_step = [[bucket_idx for _ in range(len(bucket_data) // self.params['batch_size'])]
                          for bucket_idx, bucket_data in bucketed.items()]
        bucket_at_step = [x for y in bucket_at_step for x in y]

        return (bucketed, bucket_sizes, bucket_at_step)

    def make_minibatch_iterator(self, data, is_training: bool):
        (bucketed, bucket_sizes, bucket_at_step) = data
        if is_training:
            np.random.shuffle(bucket_at_step)
            for _, bucketed_data in bucketed.items():
                np.random.shuffle(bucketed_data)

        bucket_counters = defaultdict(int)
        for step in range(len(bucket_at_step)):
            bucket = bucket_at_step[step]
            start_idx = bucket_counters[bucket] * self.params['batch_size']
            end_idx = (bucket_counters[bucket] + 1) * self.params['batch_size']
            elements = bucketed[bucket][start_idx:end_idx]
            batch_data = {'adj_mat': [], 'init': [], 'label': []}
            for d in elements:
                batch_data['adj_mat'].append(d['adj_mat'])
                batch_data['init'].append(d['init'])
                batch_data['label'].append(d['label'])

            num_graphs = len(batch_data['init'])
            initial_representations = batch_data['init']
            initial_representations = np.pad(initial_representations,
                                             pad_width=[[0, 0], [0, 0], [0, self.params['hidden_size'] - self.annotation_size]],
                                             mode='constant')
            batch_feed_dict = {
                self.placeholders['initial_node_representation']: initial_representations,
                self.placeholders['target_values']: batch_data['label'],
                self.placeholders['num_graphs']: num_graphs,
                self.placeholders['num_vertices']: bucket_sizes[bucket],
                self.placeholders['adjacency_matrix']: batch_data['adj_mat'],
            }

            yield batch_feed_dict


def main():
    args = docopt(__doc__)
    try:
        model = DenseGGNNChemModel(args)
        model.train()
    except:
        typ, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)


if __name__ == "__main__":
    main()
