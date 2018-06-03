# -*- coding: utf-8 -*-
"""
Тренировка модели определения релевантности предпосылки и вопроса.
Модель используется в проекте чат-бота https://github.com/Koziev/chatbot
Используется LightGBM.

Альтернативные модели - на базе XGBoost (xgb_relevancy.py) и нейросететевые (nn_relevancy.py)

Для запуска обучения с нужными параметрами командной строки см. ../scripts/train_lgb_relevancy.sh
"""

from __future__ import division
from __future__ import print_function

import gc
import itertools
import json
import os
import sys
import argparse
import codecs
import logging

import numpy as np
import pandas as pd
import sklearn.metrics
from sklearn.cluster import DBSCAN
from sklearn.cluster import AgglomerativeClustering
import tqdm
import lightgbm

import hyperopt
from hyperopt import hp, fmin, tpe, STATUS_OK, Trials

from scipy.sparse import lil_matrix
from sklearn.model_selection import train_test_split

from utils.tokenizer import Tokenizer
from utils.segmenter import Segmenter
from trainers.evaluation_dataset import EvaluationDataset
from trainers.evaluation_markup import EvaluationMarkup
from utils.phrase_splitter import PhraseSplitter

config_filename = 'lgb_relevancy.config'

# алгоритм сэмплирования гиперпараметров
HYPEROPT_ALGO = tpe.suggest  #  tpe.suggest OR hyperopt.rand.suggest


parser = argparse.ArgumentParser(description='LightGBM classifier for text relevance estimation')
parser.add_argument('--run_mode', type=str, default='train', help='what to do: train | evaluate | query | query2')
parser.add_argument('--hyperopt', type=int, default=0, help='use hyperopt when training')
parser.add_argument('--shingle_len', type=int, default=3, help='shingle length')
parser.add_argument('--eta', type=float, default=0.20, help='"eta" (learning rate) parameter for LightGBM')
parser.add_argument('--subsample', type=float, default=1.00, help='"subsample" parameter for LightGBM')
parser.add_argument('--num_leaves', type=int, default=31, help='"num_leaves" parameter for LightGBM')
parser.add_argument('--min_data_in_leaf', type=int, default=10, help='"min_data_in_leaf" parameter for LightGBM')
parser.add_argument('--input', type=str, default='../data/premise_question_relevancy.csv', help='path to input dataset')
parser.add_argument('--tmp', type=str, default='../tmp', help='folder to store results')
parser.add_argument('--data_dir', type=str, default='../data', help='folder containing some evaluation datasets')
parser.add_argument('--lemmatize', type=int, default=0, help='canonize phrases before extracting the shingles: 0 - none, 1 - lemmas, 2 - stems')

args = parser.parse_args()

input_path = args.input
tmp_folder = args.tmp
data_folder = args.data_dir
run_mode = args.run_mode
lemmatize = args.lemmatize
subsample = args.subsample
num_leaves = args.num_leaves
min_data_in_leaf = args.min_data_in_leaf

# количество случайных наборов параметров, проверяемых в hyperopt
# если указать 0, то hyperopt не применяется, а выполняется обучение
# с заданными параметрами (--num_leaves, --min_data_in_leaf, --eta, --subsample)
use_hyperopt = args.hyperopt

# основной настроечный параметр модели - длина символьных N-грамм (шинглов)
shingle_len = args.shingle_len
if shingle_len<2 or shingle_len>6:
    print('Invalid --shingle_len option value')
    exit(1)

eta = args.eta
if eta<0.01 or eta>=1.0:
    print('Invalid --eta option value')
    exit(1)

# -------------------------------------------------------------------

BEG_WORD = '\b'
END_WORD = '\n'


def ngrams(s, n):
    return [u''.join(z) for z in itertools.izip(*[s[i:] for i in range(n)])]


def words2str(words):
    return u' '.join(itertools.chain([BEG_WORD], words, [END_WORD]))


def vectorize_sample_x(X_data, idata, premise_shingles, question_shingles, shingle2id):
    ps = set(premise_shingles)
    qs = set(question_shingles)
    common_shingles = ps & qs
    notmatched_ps = ps - qs
    notmatched_qs = qs - ps

    nb_shingles = len(shingle2id)

    icol = 0
    for shingle in common_shingles:
        if shingle in shingle2id:
            X_data[idata, icol+shingle2id[shingle]] = True

    icol += nb_shingles
    for shingle in notmatched_ps:
        if shingle in shingle2id:
            X_data[idata, icol+shingle2id[shingle]] = True

    icol += nb_shingles
    for shingle in notmatched_qs:
        if shingle in shingle2id:
            X_data[idata, icol+shingle2id[shingle]] = True


def train_model(lgb_params, D_train, D_val, y_val):
    """
    Тренировка модели на данных D_train, валидация и early stopping на D_val и y_val.
    :param lgb_params: параметры тренировки для LightGBM
    :param D_train: тренировочные входные данные
    :param D_val: данные для валидации
    :param y_val: целевые значения для валидационного набора для расчета accuracy и F1
    :return: кортеж (бустер, acc, f1)
    """
    lgb_params['bagging_freq'] = 1

    print('Train LightGBM model with learning_rate={} num_leaves={} min_data_in_leaf={} bagging_fraction={}...'.format(lgb_params['learning_rate'],
                                                                                                                       lgb_params['num_leaves'],
                                                                                                                       lgb_params['min_data_in_leaf'],
                                                                                                                       lgb_params['bagging_fraction']))
    cl = lightgbm.train(lgb_params,
                        D_train,
                        valid_sets=[D_val],
                        valid_names=['val'],
                        num_boost_round=10000,
                        verbose_eval=50,
                        early_stopping_rounds=50)

    y_pred = cl.predict(X_val)
    y_pred = (y_pred >= 0.5).astype(np.int)

    # Точность на валидационных данных малоинформативна из-за сильного дисбаланса 1/0 классов,
    # напечатаем только для контроля кода обучения.
    acc = sklearn.metrics.accuracy_score(y_true=y_val, y_pred=y_pred)

    # из-за сильного дисбаланса (в пользу исходов с y=0) оценивать качество
    # получающейся модели лучше по f1
    f1 = sklearn.metrics.f1_score(y_true=y_val, y_pred=y_pred)

    return cl, acc, f1


def evaluate_model(lgb_relevancy, model_config, eval_data, verbose):
    xgb_relevancy_shingle2id = model_config['shingle2id']
    xgb_relevancy_shingle_len = model_config['shingle_len']
    xgb_relevancy_nb_features = model_config['nb_features']
    xgb_relevancy_lemmalize = model_config['lemmatize']

    tokenizer = PhraseSplitter.create_splitter(xgb_relevancy_lemmalize)

    nb_good = 0
    nb_bad = 0

    with codecs.open(os.path.join(tmp_folder, 'lgb_relevancy.evaluation.txt'), 'w', 'utf-8') as wrt:
        for irecord, phrases in eval_data.generate_groups():
            nb_samples = len(phrases)

            X_data = lil_matrix((nb_samples, xgb_relevancy_nb_features), dtype='float32')

            for irow, (premise_words, question_words) in enumerate(phrases):
                premise_wx = words2str(premise_words)
                question_wx = words2str(question_words)

                premise_shingles = set(ngrams(premise_wx, xgb_relevancy_shingle_len))
                question_shingles = set(ngrams(question_wx, xgb_relevancy_shingle_len))

                vectorize_sample_x(X_data, irow, premise_shingles, question_shingles, xgb_relevancy_shingle2id)

            y_pred = lgb_relevancy.predict(X_data)

            # предпосылка с максимальной релевантностью
            max_index = np.argmax(y_pred)
            selected_premise = u' '.join(phrases[max_index][0]).strip()

            # эта выбранная предпосылка соответствует одному из вариантов
            # релевантных предпосылок в этой группе?
            if eval_data.is_relevant_premise(irecord, selected_premise):
                nb_good += 1
                if verbose:
                    print(EvaluationMarkup.ok_color + EvaluationMarkup.ok_bullet + EvaluationMarkup.close_color, end='')
                    wrt.write(EvaluationMarkup.ok_bullet)
            else:
                nb_bad += 1
                if verbose:
                    print(EvaluationMarkup.fail_color + EvaluationMarkup.fail_bullet + EvaluationMarkup.close_color, end='')
                    wrt.write(EvaluationMarkup.fail_bullet)

            max_sim = np.max(y_pred)

            if verbose == 1:
                question_words = phrases[0][1]
                message_line = u'{:<40} {:<40} {}/{}'.format(u' '.join(question_words), u' '.join(phrases[max_index][0]), y_pred[max_index], y_pred[0])
                print(message_line)
                wrt.write(message_line+u'\n')

            # для отладки: top релевантных вопросов
            if False:
                print(u'Most similar premises for question {}'.format(u' '.join(question)))
                yy = [(y_pred[i], i) for i in range(len(y_pred))]
                yy = sorted(yy, key=lambda z:-z[0])

                for sim, index in yy[:5]:
                    print(u'{:.4f} {}'.format(sim, u' '.join(phrases[index][0])))

    # Итоговая точность выбора предпосылки.
    accuracy = float(nb_good)/float(nb_good+nb_bad)
    if verbose == 1:
        print('eval accuracy={}'.format(accuracy))

    return accuracy


def get_params(space):
    px = dict()
    px['boosting_type']='gbdt'
    px['objective'] ='binary'
    px['metric'] = 'binary_logloss'
    px['learning_rate']=space['learning_rate']
    px['num_leaves'] = int(space['num_leaves'])
    px['min_data_in_leaf'] = int(space['min_data_in_leaf'])
    px['min_sum_hessian_in_leaf'] = space['min_sum_hessian_in_leaf']
    px['max_depth'] = int(space['max_depth']) if 'max_depth' in space else -1
    px['lambda_l1'] = 0.0  # space['lambda_l1'],
    px['lambda_l2'] = 0.0  # space['lambda_l2'],
    px['max_bin'] = 256
    px['feature_fraction'] = space['feature_fraction']
    px['bagging_fraction'] = space['bagging_fraction']
    px['bagging_freq'] = 1

    return px



obj_call_count = 0
cur_best_acc = -np.inf
hyperopt_log_writer = None
ho_model_config = None
ho_eval_data = None

def objective(space):
    global obj_call_count, cur_best_acc

    obj_call_count += 1

    print('\nLightGBM objective call #{} cur_best_acc={:7.5f}'.format(obj_call_count, cur_best_acc) )

    lgb_params = get_params(space)

    sorted_params = sorted(space.iteritems(), key=lambda z: z[0])
    print('Params:', str.join(' ', ['{}={}'.format(k, v) for k, v in sorted_params]))

    cl, val_acc, val_f1 = train_model(lgb_params, D_train, D_val, y_val)
    eval_acc = evaluate_model(cl, ho_model_config, ho_eval_data, 0)
    print('eval_acc={}'.format(eval_acc))

    do_store = False
    if eval_acc > cur_best_acc:
        cur_best_acc = eval_acc
        do_store = True
        print(EvaluationMarkup.ok_color + 'NEW BEST ACC={}'.format(cur_best_acc) + EvaluationMarkup.close_color)

    if do_store:
        model_filename = ho_model_config['model_filename']
        cl.save_model(model_filename)
        hyperopt_log_writer.write( 'eval acc={:<7.5f} Params:{}\n'.format(eval_acc, str.join(' ', ['{}={}'.format(k, v) for k, v in sorted_params])))
        hyperopt_log_writer.flush()

    #end = time.time()
    #elapsed = int(end - start)
    ##print('elapsed={}'.format(elapsed ) )

    return{'loss':-cur_best_acc, 'status': STATUS_OK}



# -------------------------------------------------------------------

if run_mode == 'train':
    # Режим тренировки модели.
    df = pd.read_csv(input_path, encoding='utf-8', delimiter='\t', quoting=3)
    print('samples.count={}'.format(df.shape[0]))

    tokenizer = PhraseSplitter.create_splitter(lemmatize)

    all_shingles = set()

    for i,record in tqdm.tqdm(df.iterrows(), total=df.shape[0], desc='Shingles'):
        for phrase in [record['premise'], record['question']]:
            words = tokenizer.tokenize(phrase)
            wx = words2str(words)
            all_shingles.update(ngrams(wx, shingle_len))

    nb_shingles = len(all_shingles)
    print('nb_shingles={}'.format(nb_shingles))

    shingle2id = dict([(s,i) for i,s in enumerate(all_shingles)])

    phrases = []
    ys = []
    weights = []

    for index, row in tqdm.tqdm(df.iterrows(), total=df.shape[0], desc='Extract phrases'):
        weights.append(row['weight'])
        phrase1 = row['premise']
        phrase2 = row['question']
        words1 = words2str(tokenizer.tokenize(phrase1))
        words2 = words2str(tokenizer.tokenize(phrase2))

        y = row['relevance']
        if y in (0,1):
            ys.append(y)
            phrases.append( (words1, words2, phrase1, phrase2) )

    nb_patterns = len(ys)

    nb_features = nb_shingles*3
    X_data = lil_matrix((nb_patterns, nb_features), dtype='float32')
    y_data = []

    for idata, (phrase12, y12) in tqdm.tqdm(enumerate(itertools.izip(phrases, ys)),
                                            total=nb_patterns,
                                            desc='Vectorization'):
        premise = phrase12[0]
        question = phrase12[1]
        y = y12

        y_data.append(y)

        premise_shingles = ngrams(premise, shingle_len)
        question_shingles = ngrams(question, shingle_len)
        vectorize_sample_x(X_data, idata, premise_shingles, question_shingles, shingle2id)

    nb_0 = len(filter(lambda y: y == 0, y_data))
    nb_1 = len(filter(lambda y: y == 1, y_data))

    print('nb_0={}'.format(nb_0))
    print('nb_1={}'.format(nb_1))

    SEED = 123456
    TEST_SHARE = 0.2
    X_train, X_val, y_train, y_val, w_train, w_val = train_test_split(X_data,
                                                                      y_data,
                                                                      weights,
                                                                      test_size=TEST_SHARE,
                                                                      random_state=SEED)

    D_train = lightgbm.Dataset(data=X_train, label=y_train, weight=w_train, silent=1)
    D_val = lightgbm.Dataset(data=X_val, label=y_val, weight=w_val, silent=1)

    del X_train
    del X_data
    del df
    gc.collect()

    model_filename = os.path.join( tmp_folder, 'lgb_relevancy.model' )

    # сохраним конфиг модели, чтобы ее использовать в чат-боте
    model_config = {
                    'model': 'lightgbm',
                    'shingle2id': shingle2id,
                    'model_filename': model_filename,
                    'shingle_len': shingle_len,
                    'nb_features': nb_features,
                    'lemmatize': lemmatize
                   }

    with open(os.path.join(tmp_folder, config_filename), 'w') as f:
        json.dump(model_config, f)

    if use_hyperopt:
        ho_model_config = model_config

        ho_eval_data = EvaluationDataset(0, tokenizer)
        ho_eval_data.load(data_folder)

        space = {
                 'num_leaves': hp.quniform('num_leaves', 20, 100, 1),
                 'min_data_in_leaf': hp.quniform('min_data_in_leaf', 5, 100, 1),
                 'feature_fraction': hp.uniform('feature_fraction', 0.75, 1.0),
                 'bagging_fraction': hp.uniform('bagging_fraction', 0.75, 1.0),
                 'learning_rate': hp.loguniform('learning_rate', -2, -1.2),
                 'min_sum_hessian_in_leaf': hp.loguniform('min_sum_hessian_in_leaf', 0, 2.3),
                }

        hyperopt_log_writer = open(os.path.join(tmp_folder, 'lgb_relevancy.hyperopt.txt'), 'w')

        trials = Trials()
        best = hyperopt.fmin(fn=objective,
                             space=space,
                             algo=HYPEROPT_ALGO,
                             max_evals=use_hyperopt,
                             trials=trials,
                             verbose=1)

        hyperopt_log_writer.close()
    else:
        lgb_params = dict()
        lgb_params['boosting_type']='gbdt'
        lgb_params['objective'] ='binary'
        lgb_params['metric'] = 'binary_logloss'
        lgb_params['learning_rate'] = eta
        lgb_params['num_leaves'] = num_leaves
        lgb_params['min_data_in_leaf'] = min_data_in_leaf
        lgb_params['min_sum_hessian_in_leaf'] = 1
        lgb_params['max_depth'] = -1
        lgb_params['lambda_l1'] = 0.0  # space['lambda_l1'],
        lgb_params['lambda_l2'] = 0.0  # space['lambda_l2'],
        lgb_params['max_bin'] = 256
        lgb_params['feature_fraction'] = 1.0
        lgb_params['bagging_fraction'] = subsample
        lgb_params['bagging_freq'] = 1

        cl, acc, f1 = train_model(lgb_params, D_train, D_val, y_val)

        print('Training has finished')
        print('val acc={}'.format(acc))
        print('val f1={}'.format(f1))

        # сохраняем саму модель
        cl.save_model( model_filename )


if run_mode == 'evaluate':
    # Оценка качества натренированной модели на специальном наборе вопросов и
    # ожидаемых выборов предпосылок из отдельного тренировочного набора.

    # Загружаем данные обученной модели.
    with open(os.path.join(tmp_folder, config_filename), 'r') as f:
        model_config = json.load(f)

    tokenizer = PhraseSplitter.create_splitter(model_config['lemmatize'])

    eval_data = EvaluationDataset(0, tokenizer)
    eval_data.load(data_folder)

    lgb_relevancy = lightgbm.Booster(model_file=model_config['model_filename'])

    accuracy = evaluate_model(lgb_relevancy, model_config, eval_data, verbose=1)

    # Итоговая точность выбора предпосылок на оценочной задаче.
    print('eval accuracy={}'.format(accuracy))

if run_mode == 'clusterize':
    # семантическая кластеризация предложений с использованием
    # обученной модели в качестве калькулятора метрики попарной близости.

    # Загружаем данные обученной модели.
    with open(os.path.join(tmp_folder, config_filename), 'r') as f:
        model_config = json.load(f)

    lgb_relevancy_shingle2id = model_config['shingle2id']
    lgb_relevancy_shingle_len = model_config['shingle_len']
    lgb_relevancy_nb_features = model_config['nb_features']
    lgb_relevancy_lemmalize = model_config['lemmatize']

    tokenizer = PhraseSplitter.create_splitter(lgb_relevancy_lemmalize)

    lgb_relevancy = lightgbm.Booster(model_file=model_config['model_filename'])

    # в качестве источника предложений возьмем обучающий датасет. из которого возьмем
    # релевантные предпосылки и вопросы
    df = pd.read_csv(input_path, encoding='utf-8', delimiter='\t', quoting=3)

    phrases = set()
    for i, row in tqdm.tqdm(df.iterrows(), total=df.shape[0], desc='Extract phrases'):
        if row['relevance'] == 1:
            for phrase in [row['question'], row['premise']]:
                words = tokenizer.tokenize(phrase)
                wx = words2str(words)
                phrases.add((wx, phrase))

    # оставим небольшую часть предложений, чтобы ограничить количество попарных дистанций
    phrases = np.random.permutation(list(phrases))[:2000]
    nb_phrases = len(phrases)

    print('Computation of {0}*{0} distance matrix'.format(nb_phrases))

    distances = np.zeros((nb_phrases, nb_phrases), dtype='float32')

    min_dist = np.inf
    max_dist = -np.inf

    # в принципе, достаточно вычислить верхнетреугольную матрицу расстояний.
    for i1, (phrase1, _) in tqdm.tqdm(enumerate(phrases[:-1]), total=nb_phrases-1, desc='Distance matrix'):
        shingles1 = set(ngrams(phrase1, lgb_relevancy_shingle_len))
        n2 = nb_phrases-i1-1
        X_data = lil_matrix((n2, lgb_relevancy_nb_features), dtype='float32')

        for i2, (phrase2, _) in enumerate(phrases[i1+1:]):
            shingles2 = set(ngrams(phrase2, lgb_relevancy_shingle_len))
            vectorize_sample_x(X_data, i2, shingles1, shingles2, lgb_relevancy_shingle2id)

        y_pred = lgb_relevancy.predict(X_data)
        for i2 in range(i1+1, nb_phrases):
            y = 1.0-y_pred[i2-i1-1]
            distances[i1, i2] = y
            distances[i2, i1] = y
            min_dist = min( min_dist, y)
            max_dist = max( max_dist, y)

    print('\nmin_dist={} max_dist={}'.format(min_dist, max_dist))

    print('Clusterization...')
    if False:
        # http://scikit-learn.org/dev/auto_examples/cluster/plot_dbscan.html#sphx-glr-auto-examples-cluster-plot-dbscan-py
        cl = sklearn.cluster.DBSCAN(eps=0.1, min_samples=5, metric='precomputed',
                                    metric_params=None, algorithm='auto',
                                    leaf_size=10, p=None, n_jobs=2)

        db = cl.fit(distances)
        labels = db.labels_
    else:
        cl = sklearn.cluster.AgglomerativeClustering(n_clusters=400, affinity='precomputed',
                                                     memory=None, connectivity=None,
                                                     compute_full_tree='auto', linkage='complete')
        cl.fit(distances)
        labels = cl.labels_

    # Number of clusters in labels, ignoring noise if present.
    n_clusters_ = len(set(labels)) - (1 if -1 in labels else 0)
    print('Number of clusters={}'.format(n_clusters_))

    with codecs.open(os.path.join(tmp_folder, 'lgb_relevancy_clusters.txt'), 'w', 'utf-8') as wrt:
        for icluster in range(n_clusters_):
            wrt.write('=== CLUSTER #{} ===\n'.format(icluster))

            for iphrase, label in enumerate(labels):
                if label == icluster:
                    wrt.write(u'{}\n'.format(phrases[iphrase][1]))

            wrt.write('\n\n')
