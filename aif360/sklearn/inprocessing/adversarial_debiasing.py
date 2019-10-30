import numpy as np
import scipy.special
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.preprocessing import LabelEncoder
from sklearn.utils import check_random_state
from sklearn.utils.validation import check_is_fitted
import tensorflow as tf

from aif360.sklearn.utils import check_inputs, check_groups


class AdversarialDebiasing(BaseEstimator, ClassifierMixin):
    """Adversarial debiasing is an in-processing technique that learns a
    classifier to maximize prediction accuracy and simultaneously reduce an
    adversary's ability to determine the protected attribute from the
    predictions [#zhang18]_. This approach leads to a fair classifier as the
    predictions cannot carry any group discrimination information that the
    adversary can exploit.

    References:
        .. [#zhang18] B. H. Zhang, B. Lemoine, and M. Mitchell, "Mitigating
           Unwanted Biases with Adversarial Learning," AAAI/ACM Conference on
           Artificial Intelligence, Ethics, and Society, 2018.
    """

    def __init__(self, prot_attr=None, scope_name='classifier',
                 adversary_loss_weight=0.1, num_epochs=50, batch_size=128,
                 classifier_num_hidden_units=200, debias=True, verbose=False,
                 random_state=None):

        self.prot_attr = prot_attr
        self.scope_name = scope_name
        self.adversary_loss_weight = adversary_loss_weight
        self.num_epochs = num_epochs
        self.batch_size = batch_size
        self.classifier_num_hidden_units = classifier_num_hidden_units
        self.debias = debias
        self.verbose = verbose
        self.random_state = random_state

    def fit(self, X, y):
        X, y, _ = check_inputs(X, y)
        rng = check_random_state(self.random_state)
        ii32 = np.iinfo(np.int32)
        seed1, seed2, seed3, seed4 = rng.randint(ii32.min, ii32.max, size=4)

        tf.reset_default_graph()
        self.sess_ = tf.Session()

        groups, self.prot_attr_ = check_groups(X, self.prot_attr)
        le = LabelEncoder()
        y = le.fit_transform(y)
        self.classes_ = le.classes_
        groups = groups.map(str)  # BUG: LabelEncoder converts to ndarray which removes tuple formatting
        groups = le.fit_transform(groups)
        self.groups_ = le.classes_

        n_classes = len(self.classes_)
        n_groups = len(self.groups_)
        # use sigmoid for binary case
        if n_classes == 2:
            n_classes = 1
        if n_groups == 2:
            n_groups = 1

        n_samples, n_features = X.shape

        with tf.variable_scope(self.scope_name):
            # Setup placeholders
            self.input_ph = tf.placeholder(tf.float32, shape=[None, n_features])
            self.prot_attr_ph = tf.placeholder(tf.float32, shape=[None, 1])
            self.true_labels_ph = tf.placeholder(tf.float32, shape=[None, 1])
            self.keep_prob = tf.placeholder(tf.float32)

            # Create classifier
            with tf.variable_scope('classifier_model'):
                W1 = tf.get_variable(
                        'W1', [n_features, self.classifier_num_hidden_units],
                        initializer=tf.initializers.glorot_uniform(seed=seed1))
                b1 = tf.Variable(tf.zeros(shape=[self.classifier_num_hidden_units]),
                        name='b1')

                h1 = tf.nn.relu(tf.matmul(self.input_ph, W1) + b1)
                h1 = tf.nn.dropout(h1, rate=1-self.keep_prob, seed=seed2)

                W2 = tf.get_variable(
                        'W2', [self.classifier_num_hidden_units, n_classes],
                        initializer=tf.initializers.glorot_uniform(seed=seed3))
                b2 = tf.Variable(tf.zeros(shape=[n_classes]), name='b2')

                self.classifier_logits_ = tf.matmul(h1, W2) + b2

            # Obtain classifier loss
            if self.classifier_logits_.shape[1] == 1:
                clf_loss = tf.reduce_mean(
                        tf.nn.sigmoid_cross_entropy_with_logits(
                                labels=self.true_labels_ph,
                                logits=self.classifier_logits_))
            else:
                clf_loss = tf.reduce_mean(
                        tf.nn.sparse_softmax_cross_entropy_with_logits(
                                labels=tf.squeeze(tf.cast(self.true_labels_ph,
                                                          tf.int32)),
                                logits=self.classifier_logits_))

            if self.debias:
                # Create adversary
                with tf.variable_scope("adversary_model"):
                    c = tf.get_variable('c', initializer=tf.constant(1.0))
                    s = tf.sigmoid((1 + tf.abs(c)) * self.classifier_logits_)

                    W2 = tf.get_variable('W2', [3, n_groups],
                            initializer=tf.initializers.glorot_uniform(seed=seed4))
                    b2 = tf.Variable(tf.zeros(shape=[n_groups]), name='b2')

                    self.adversary_logits_ = tf.matmul(
                            tf.concat([s, s * self.true_labels_ph,
                                       s * (1.0 - self.true_labels_ph)], axis=1),
                            W2) + b2

                # Obtain adversary loss
                if self.adversary_logits_.shape[1] == 1:
                    adv_loss = tf.reduce_mean(
                            tf.nn.sigmoid_cross_entropy_with_logits(
                                    labels=self.prot_attr_ph,
                                    logits=self.adversary_logits_))
                else:
                    adv_loss = tf.reduce_mean(
                            tf.nn.sparse_softmax_cross_entropy_with_logits(
                                    labels=tf.squeeze(tf.cast(self.prot_attr_ph,
                                                              tf.int32)),
                                    logits=self.adversary_logits_))

            global_step = tf.train.get_or_create_global_step()
            starter_learning_rate = 0.001
            learning_rate = tf.train.exponential_decay(starter_learning_rate,
                    global_step, 1000, 0.96, staircase=True)

            # Setup optimizers
            clf_opt = tf.train.AdamOptimizer(learning_rate)
            if self.debias:
                adv_opt = tf.train.AdamOptimizer(learning_rate)

            clf_vars = [var for var in tf.trainable_variables()
                        if 'classifier_model' in var.name]
            if self.debias:
                adv_vars = [var for var in tf.trainable_variables()
                            if 'adversary_model' in var.name]
                # Compute grad wrt classifier parameters
                adv_grads = {var: grad for (grad, var) in
                        adv_opt.compute_gradients(adv_loss, var_list=clf_vars)}

            normalize = lambda x: x / (tf.norm(x) + np.finfo(np.float32).tiny)

            clf_grads = []
            for (grad, var) in clf_opt.compute_gradients(clf_loss, var_list=clf_vars):
                if self.debias:
                    unit_adv_grad = normalize(adv_grads[var])
                    # proj_{adv_grad} clf_grad:
                    grad -= tf.reduce_sum(grad * unit_adv_grad) * unit_adv_grad
                    grad -= self.adversary_loss_weight * adv_grads[var]
                clf_grads.append((grad, var))

            clf_min = clf_opt.apply_gradients(clf_grads, global_step=global_step)
            if self.debias:
                with tf.control_dependencies([clf_min]):
                    adv_min = adv_opt.minimize(adv_loss, var_list=adv_vars)

            self.sess_.run(tf.global_variables_initializer())

            # Begin training
            for epoch in range(self.num_epochs):
                shuffled_ids = rng.permutation(n_samples)
                for i in range(n_samples // self.batch_size):
                    batch_ids = shuffled_ids[self.batch_size * i:
                                             self.batch_size * (i+1)]
                    batch_features = X.iloc[batch_ids]
                    batch_labels = y[batch_ids][:, np.newaxis]
                    batch_prot_attr = groups[batch_ids][:, np.newaxis]
                    batch_feed_dict = {self.input_ph: batch_features,
                                       self.true_labels_ph: batch_labels,
                                       self.prot_attr_ph: batch_prot_attr,
                                       self.keep_prob: 0.8}
                    if self.debias:
                        _, _, clf_loss_value, adv_loss_value = (
                                self.sess_.run([clf_min, adv_min,
                                               clf_loss, adv_loss],
                                               feed_dict=batch_feed_dict))
                        if i % 200 == 0 and self.verbose:
                            print("epoch {}; iter: {}; batch classifier loss: "
                                  "{}; batch adversarial loss: {}".format(
                                          epoch, i, clf_loss_value,
                                          adv_loss_value))
                    else:
                        _, clf_loss_value = self.sess_.run(
                                [clf_min, clf_loss],
                                feed_dict=batch_feed_dict)
                        if i % 200 == 0 and self.verbose:
                            print("epoch {}; iter: {}; batch classifier loss: "
                                  "{}".format(epoch, i, clf_loss_value))

        return self

    def decision_function(self, X):
        check_is_fitted(self, ['classes_', 'input_ph', 'keep_prob',
                               'classifier_logits_'])
        n_samples = X.shape[0]
        n_classes = len(self.classes_)
        if n_classes == 2:
            n_classes = 1

        samples_covered = 0
        scores = np.empty((n_samples, n_classes))
        while samples_covered < n_samples:
            start = samples_covered
            end = samples_covered + self.batch_size
            if end > n_samples:
                end = n_samples

            batch_ids = np.arange(start, end)
            batch_features = X.iloc[batch_ids]

            batch_feed_dict = {self.input_ph: batch_features,
                               self.keep_prob: 1.0}

            scores[batch_ids] = self.sess_.run(self.classifier_logits_,
                                              feed_dict=batch_feed_dict)
            samples_covered += len(batch_features)

        return scores.ravel() if scores.shape[1] == 1 else scores

    def predict_proba(self, X):
        decision = self.decision_function(X)

        if decision.ndim == 1:
            decision_2d = np.c_[np.zeros_like(decision), decision]
        else:
            decision_2d = decision
        return scipy.special.softmax(decision_2d, axis=1)

    def predict(self, X):
        scores = self.decision_function(X)
        if scores.ndim == 1:
            indices = (scores > 0).astype(np.int)
        else:
            indices = scores.argmax(axis=1)
        return self.classes_[indices]
