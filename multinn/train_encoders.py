"""Training the MultINN Encoders."""

import argparse
import os
import time

import numpy as np
import tensorflow as tf

from models.multinn.multinn import MultINN
from utils.auxiliary import time_to_str
from utils.data import load_data
from utils.setup import setup, get_logger
from utils.training import TrainingStats, LossAccumulator, collect_metrics


def main(args):
    # Setting up an experiment
    config, params = setup(args)

    # Setting up logger
    logger = get_logger(config['model_name'], config['dirs']['logs_dir'])

    # Extracting configurations
    data_config = config['data']
    logs_config = config['logs']
    training_config = config['training']
    dirs_config = config['dirs']
    logger.info('[SETUP] Experiment configurations')
    logger.info(f'[SETUP] Experiment directory: {os.path.abspath(dirs_config["exp_dir"])}')

    # Loading the dataset
    (X_train, len_train), (X_valid, len_valid), (_, _) = load_data(
        data_config=data_config,
        step_size=training_config['num_pixels']
    )
    logger.info(f'[LOAD]  Dataset (shape: {X_train[0].shape})')

    # Computing beat size in time steps
    beat_size = float(data_config['beat_resolution'] / training_config['num_pixels'])

    # Creating the MultINN model
    tf.reset_default_graph()
    model = MultINN(config, params, mode=params['mode'], name=config['model_name'])
    logger.info('[BUILT] Model')

    # Building optimizer and training ops
    if args.sgd:
        optimizer = tf.train.GradientDescentOptimizer(learning_rate=training_config['learning_rate'])
    else:
        optimizer = tf.train.AdamOptimizer(learning_rate=training_config['learning_rate'], epsilon=1e-4)

    init_ops, update_ops, metrics, metrics_upd, summaries = model.train_encoders(
        optimizer=optimizer, lr=training_config['learning_rate'], layer=args.layer
    )
    logger.info('[BUILT] Optimizer and update ops')

    # Extracting placeholders, metrics and summaries
    placeholders = model.placeholders
    x, lengths, is_train = placeholders['x'], placeholders['lengths'], placeholders['is_train']

    loss = metrics['batch/loss']
    loglik = metrics['log_likelihood']

    weights_sum, metrics_sum, gradients_sum = summaries['weights'], summaries['metrics'], summaries['gradients']

    # TensorFlow Session set up
    tf_config = tf.ConfigProto()
    tf_config.gpu_options.allow_growth = True
    tf.set_random_seed(training_config['random_seed'])
    np.random.seed(training_config['random_seed'])

    with tf.Session(config=tf_config) as sess:
        logger.info('[START] TF Session')
        with tf.variable_scope('init_global'):
            init_global = tf.global_variables_initializer()
        with tf.variable_scope('init_local'):
            init_local = tf.local_variables_initializer()
        sess.run([init_global, init_local])

        stats = TrainingStats()

        # Loading the model's weights or using initial weights
        if not args.from_init:
            if args.from_last:
                if model.load(sess, dirs_config['encoders_last_dir']):
                    last_stats_file = os.path.join(dirs_config['encoders_last_dir'], 'steps')
                    if os.path.isfile(last_stats_file):
                        stats.load(last_stats_file)
                        logger.info('[LOAD]  Training stats file')

                    logger.info(f'[LOAD]  Pre-trained weights (last, epoch={stats.epoch})')
                else:
                    logger.info('[LOAD]  Initial weights')
            elif model.load(sess, dirs_config['encoders_dir']):
                if os.path.isfile(dirs_config['encoders_stats_file']):
                    stats.load(dirs_config['encoders_stats_file'])
                    logger.info('[LOAD]  Training file')

                logger.info(f'[LOAD]  Pre-trained weights (best, epoch={stats.epoch})')
            else:
                logger.info('[LOAD]  Initial weights')

                # run initialization update if exists
                if init_ops:
                    sess.run(init_ops, feed_dict={x: X_train[:1600]})
                    logger.info('[END]   Run initialization ops')

        stats.new_run()

        # Preparing to the training
        graph = sess.graph if logs_config['save_graph'] else None
        writer_train = tf.summary.FileWriter(f'{dirs_config["logs_dir"]}/Graph/encoders/run_{stats.run}/train', graph)
        writer_valid = tf.summary.FileWriter(f'{dirs_config["logs_dir"]}/Graph/encoders/run_{stats.run}/valid')

        batch_size = training_config['batch_size']
        piece_size = int(training_config['piece_size'] * beat_size)

        logger.info(f'[START] Training, RUN={stats.run}')
        ids = np.arange(X_train.shape[0])

        # Logging initial weights
        if logs_config['log_weights_steps'] > 0:
            writer_train.add_summary(sess.run(weights_sum), stats.steps)
            logger.info('[LOG]   Initial weights')

        loss_accum = LossAccumulator()

        # Training on all of the songs `num_epochs` times
        past_epochs = stats.epoch
        for epoch in range(past_epochs + 1, past_epochs + training_config['epochs'] + 1):
            stats.new_epoch()
            tf.set_random_seed(epoch)
            np.random.seed(epoch)

            start = time.time()

            np.random.shuffle(ids)
            loss_accum.clear()

            for i in range(0, X_train.shape[0], batch_size):
                for j in range(0, X_train.shape[1], piece_size):
                    len_batch = len_train[ids[i:i + batch_size]] - j
                    non_empty = np.where(len_batch > 0)[0]

                    if len(non_empty) > 0:
                        len_batch = np.minimum(len_batch[non_empty], piece_size)
                        max_length = len_batch.max()

                        songs_batch = X_train[ids[i:i + batch_size], j:j + max_length, ...][non_empty]

                        if logs_config['log_weights_steps'] > 0 \
                                and (stats.steps + 1) % logs_config['log_weights_steps'] == 0 \
                                and j + piece_size >= X_train.shape[1]:
                            _, loss_i, summary = sess.run([update_ops, loss, weights_sum],
                                                          feed_dict={x: songs_batch,
                                                                     lengths: len_batch,
                                                                     is_train: True})

                            writer_train.add_summary(summary, stats.steps + 1)
                            del summary
                        else:
                            _, loss_i = sess.run([update_ops, loss],
                                                 feed_dict={x: songs_batch,
                                                            lengths: len_batch,
                                                            is_train: True})

                        del songs_batch
                        loss_accum.update(loss_i)

                stats.new_step()

            # Log the progress during training
            logger.info(f'[STAT]  Epoch {epoch}  '
                        f'(time: {time_to_str(time.time() - start)})  '
                        f'(loss: {loss_accum.loss():.3f})')

            # Evaluating the model on the training and validation data
            if logs_config['evaluate_epochs'] > 0 and epoch % logs_config['evaluate_epochs'] == 0:
                num_eval = X_valid.shape[0]

                collect_metrics(sess, metrics_upd, data=X_train[:num_eval, ...], data_lengths=len_train[:num_eval, ...],
                                placeholders=placeholders, batch_size=batch_size * 2, piece_size=piece_size)
                summary, loglik_val = sess.run([metrics_sum, loglik])
                writer_train.add_summary(summary, epoch)
                del summary
                logger.info(f'[EVAL]  Training   set log-likelihood:  enc.={loglik_val:7.3f}')

                collect_metrics(sess, metrics_upd, data=X_valid, data_lengths=len_valid, placeholders=placeholders,
                                batch_size=batch_size * 2, piece_size=piece_size)
                summary, loglik_val = sess.run([metrics_sum, loglik])
                writer_valid.add_summary(summary, epoch)
                del summary
                logger.info(f'[EVAL]  Validation set log-likelihood:  enc.={loglik_val:7.3f}')

            # Saving the model if the monitored metric decreased
            if loglik_val < stats.metric_best:
                stats.update_metric_best(loglik_val)
                stats.reset_idle_epochs()

                if logs_config['save_checkpoint_epochs'] > 0 and epoch % logs_config['save_checkpoint_epochs'] == 0:
                    model.save(sess, dirs_config['encoders_dir'], global_step=stats.steps)
                    stats.save(dirs_config['encoders_stats_file'])

                    logger.info(f'[SAVE]  Saved model after {epoch} epoch(-s) ({stats.steps} steps)')
            else:
                stats.new_idle_epoch()
                if stats.idle_epochs >= training_config['early_stopping']:
                    # Early stopping after no improvement
                    logger.info(f'[WARN]  No improvement after {training_config["early_stopping"]} epochs, quiting')
                    break

        if not args.save_best_only:
            model.save(sess, dirs_config['encoders_last_dir'], global_step=stats.steps)
            stats.save(os.path.join(dirs_config['encoders_last_dir'], 'steps'))
            logger.info(f'[SAVE]  Saved model after {epoch} epoch(-s) ({stats.steps} steps)')

        writer_train.close()
        writer_valid.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Train the MultINN Encoders')

    parser.add_argument('--model-name', '-m', metavar='M', type=str,
                        help='the name of the model (experiment)',
                        default='MultINN')
    parser.add_argument('--config', '-c', metavar='C', type=str,
                        help='experiment configuration file path',
                        default='configs/test_config.yaml')
    parser.add_argument('--params', '-p', metavar='P', type=str,
                        help='model parameters file path',
                        default='configs/test_params.yaml')
    parser.add_argument('--reuse-config',
                        help='reuse saved configurations and ignore passed',
                        action='store_true')

    parser.add_argument('--from-init',
                        help='run the model with initial weights',
                        action='store_true')
    parser.add_argument('--from-last',
                        help='run the model with the last saved weights',
                        action='store_true')
    parser.add_argument('--epochs', '-e', metavar='E', type=int,
                        help='the number of epochs to train the model',
                        default=None)
    parser.add_argument('--lr', '-lr', metavar='L', type=float,
                        help='learning rate',
                        default=None)
    parser.add_argument('--layer', '-l', metavar='L', type=int,
                        help='layer of the model to pre-train',
                        default=0)
    parser.add_argument('--sgd', '-sgd',
                        help='use SGD instead of Adam optimizer',
                        action='store_true')
    parser.add_argument('--save-best-only', '-sb',
                        help='save only the best weights based on the monitored metric',
                        action='store_true')

    args = parser.parse_args()
    print(args)

    main(args)
