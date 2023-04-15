from .switchinference import SwitchInferenceTrainer
from ..layers import DeepLightEmbedding


class DeepLightTrainer(SwitchInferenceTrainer):
    def assert_use_multi(self):
        assert self.use_multi == self.separate_fields == 0

    def get_embed_layer(self):
        real_dim = self.compress_rate * self.embedding_dim
        if real_dim >= 3:
            form = 'csr'
            real_target_sparse = (real_dim - 1) / 2 / self.embedding_dim
        else:
            form = 'coo'
            real_target_sparse = self.compress_rate / 3
        prune_rate = 1 - real_target_sparse
        self.log_func(
            f'Use {form} for sparse storage; final prune rate {prune_rate}, given target sparse rate {self.compress_rate}.')
        return DeepLightEmbedding(
            self.num_embed,
            self.embedding_dim,
            prune_rate,
            form,
            warm=self.embedding_args['warm'],
            initializer=self.initializer,
            name='DeepLightEmb',
            ctx=self.ectx,
        )

    def get_eval_nodes(self):
        embed_input, dense_input, y_ = self.data_ops
        loss, prediction = self.model(
            self.embed_layer(embed_input), dense_input, y_)
        train_op = self.opt.minimize(loss)
        eval_nodes = {
            'train': [loss, prediction, y_, train_op, self.embed_layer.make_prune_op(y_)],
            'validate': [loss, prediction, y_],
        }
        return eval_nodes

    def get_eval_nodes_inference(self, load_value=True):
        # check inference; use sparse embedding
        embed_input, dense_input, y_ = self.data_ops
        test_embed_input = self.embed_layer.make_inference(
            embed_input, load_value)
        test_loss, test_prediction = self.model(
            test_embed_input, dense_input, y_)
        eval_nodes = {'validate': [test_loss, test_prediction, y_]}
        return eval_nodes
