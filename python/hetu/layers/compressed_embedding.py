from .base import BaseLayer
import hetu as ht
import math
import numpy as np
import os.path as osp


class HashEmbedding(BaseLayer):
    def __init__(self, num_embeddings, embedding_dim, compress_rate=None, size_limit=None, initializer=ht.init.GenXavierNormal(), name='embedding', ctx=None):
        assert compress_rate is None or size_limit is None
        if size_limit is not None:
            real_num_embeds = size_limit // embedding_dim
        else:
            if compress_rate is None:
                compress_rate = 1.0
            real_num_embeds = int(num_embeddings * compress_rate)
        self.num_embeddings = num_embeddings
        self.real_num_embeds = real_num_embeds
        self.embedding_dim = embedding_dim
        self.name = name
        self.ctx = ctx
        self.embedding_table = initializer(
            shape=(self.real_num_embeds, self.embedding_dim), name=self.name, ctx=ctx)

    def __call__(self, x):
        # ref MLSys20, HierPS
        with ht.context(self.ctx):
            sparse_input = ht.mod_hash_op(x, self.real_num_embeds)
            return ht.embedding_lookup_op(self.embedding_table, sparse_input)


class CompositionalEmbedding(BaseLayer):
    # compositional embedding
    def __init__(self, num_embeddings, embedding_dim, num_tables, aggregator, initializer=ht.init.GenXavierNormal(), name='embedding', ctx=None):
        aggregator = aggregator[:3]
        assert aggregator in ('sum', 'mul', 'con')
        self.aggregator = aggregator
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        self.num_tables = num_tables
        self.name = name
        self.ctx = ctx
        self.table_num_embedding = math.ceil(pow(num_embeddings, 1/num_tables))
        shape = (num_tables * self.table_num_embedding, self.embedding_dim)
        self.embedding_table = initializer(
            shape=shape, name=self.name)

    def __call__(self, x):
        # KDD20, CompositionalHash
        # CIKM21, BinaryCodeHash
        # x's shape: (batch_size, slot)
        with ht.context(self.ctx):
            sparse_input = ht.compo_hash_op(
                x, self.num_tables, self.table_num_embedding)
            # (batch_size, slot, ntable)
            sparse_data = ht.embedding_lookup_op(
                self.embedding_table, sparse_input)
        # (batch_size, slot, ntable, dim)
        if self.aggregator == 'sum':
            # sum
            return ht.reduce_sum_op(sparse_data, axes=[2], keepdims=False)
        elif self.aggregator == 'mul':
            # multiply
            return ht.reduce_mul_op(sparse_data, axes=[2], keepdims=False)
        elif self.aggregator == 'con':
            # concatenate
            # (batch_size, num_tables, ...), need_reshape
            return ht.transpose_op(sparse_data, [0, 2, 1, 3])


class LearningEmbedding(BaseLayer):
    # deep learning embedding
    def __init__(self, embedding_dim, num_buckets, num_hash, mlp_dim=1024, dist='uniform', initializer=ht.init.GenXavierNormal(), name='embedding', ctx=None):
        from .linear import Linear
        from .normalization import BatchNorm
        from .relu import Relu
        from .sequence import Sequence
        assert dist in ('uniform', 'normal')
        self.distribution = dist
        self.embedding_dim = embedding_dim
        self.num_buckets = num_buckets
        self.num_hash = num_hash
        self.mlp_dim = mlp_dim
        self.name = name
        self.ctx = ctx
        self.slopes = ht.Variable(name='slopes', value=np.random.randint(
            0, num_buckets, size=num_hash), trainable=False)
        self.biases = ht.Variable(name='biases', value=np.random.randint(
            0, num_buckets, size=num_hash), trainable=False)
        prime_path = osp.join(osp.dirname(osp.abspath(
            __file__)), 'primes.npy')
        allprimes = np.load(prime_path)
        for i, p in enumerate(allprimes):
            if p >= num_buckets:
                break
        allprimes = allprimes[i:]
        self.primes = ht.Variable(name='primes', value=np.random.choice(
            allprimes, size=num_hash), trainable=False)
        self.layers = Sequence(
            Linear(self.num_hash, self.mlp_dim),
            BatchNorm(self.mlp_dim),
            Relu(),
            Linear(self.mlp_dim, self.mlp_dim),
            BatchNorm(self.mlp_dim),
            Relu(),
            Linear(self.mlp_dim, self.mlp_dim),
            BatchNorm(self.mlp_dim),
            Relu(),
            Linear(self.mlp_dim, self.mlp_dim),
            BatchNorm(self.mlp_dim),
            Relu(),
            Linear(self.mlp_dim, self.embedding_dim),
            BatchNorm(self.embedding_dim),
            Relu(),
        )  # TODO: use mish instead

    def __call__(self, x):
        # KDD21, DHE
        x = ht.learn_hash_op(x, self.slopes, self.biases,
                             self.primes, self.num_buckets, self.distribution)
        x = ht.array_reshape_op(x, (-1, self.num_hash))
        x = self.layers(x)
        return x


class DPQEmbedding(BaseLayer):
    def __init__(self, num_embeddings, embedding_dim, num_choices, num_parts, num_slot, batch_size, share_weights=False, mode='vq', initializer=ht.init.GenXavierNormal(), name='embedding', ctx=None):
        from ..initializers import nulls
        from .normalization import BatchNorm
        assert mode in ('vq', 'sx')
        assert embedding_dim % num_parts == 0
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        self.num_choices = num_choices
        self.num_parts = num_parts
        self.num_slot = num_slot
        self.batch_size = batch_size
        self.share_weights = share_weights
        self.mode = mode
        self.part_embedding_dim = embedding_dim // num_parts
        self.name = name
        self.ctx = ctx
        self.embedding_table = initializer(shape=(
            num_embeddings, self.embedding_dim), name='{}_query'.format(name), ctx=ctx)
        self.key_matrix = self.make_matries(initializer, name+'_key')
        if mode == 'vq':
            self.value_matrix = self.key_matrix
        else:
            self.value_matrix = self.make_matries(initializer, name+'_value')
        self.bn_layer = BatchNorm(
            self.num_choices, scale=False, bias=False, name='{}_bn'.format(name))
        self.codebooks = nulls(shape=(num_embeddings, self.num_parts), name='{}_codebook'.format(
            name), ctx=ctx, trainable=False)
        if not self.share_weights:
            dbase = np.array(
                [self.num_choices * d for d in range(self.num_parts)], dtype=int)
            dbase = np.tile(dbase, [self.batch_size * self.num_slot, 1])
            dbase = ht.array(dbase, ctx=self.ctx)
            self.dbase = ht.placeholder_op(
                'dbase', value=dbase, trainable=False)

    def make_matries(self, initializer, name):
        if self.share_weights:
            shape = (self.num_choices, self.part_embedding_dim)
        else:
            shape = (self.num_parts * self.num_choices,
                     self.part_embedding_dim)
        return initializer(shape=shape, name='{}'.format(name), ctx=self.ctx)

    def __call__(self, x):
        with ht.context(self.ctx):
            # table: (nembed, dim), x: (bs, slot)
            query_lookups = ht.embedding_lookup_op(
                self.embedding_table, x)
            # (bs, slot, dim)
            inputs = ht.array_reshape_op(
                query_lookups, (-1, self.num_parts, self.part_embedding_dim))
            query_lookups = ht.array_reshape_op(
                query_lookups, (-1, self.num_parts, 1, self.part_embedding_dim))
            # (bs * slot, npart, 1, pdim)
            query_lookups = ht.tile_op(query_lookups, [self.num_choices, 1])
            # (bs * slot, npart, nkey, pdim)
            key_mat = ht.array_reshape_op(
                self.key_matrix, (-1, self.num_choices, self.part_embedding_dim))
            key_mat = ht.broadcastto_op(key_mat, query_lookups)
            # (bs * slot, npart, nkey, pdim)
            if self.mode == 'vq':
                # query metric: euclidean
                diff = ht.minus_op(query_lookups, key_mat)
                resp = ht.power_op(diff, 2)
                resp = ht.reduce_sum_op(resp, axes=[3])
                resp = ht.opposite_op(resp)
                # (bs * slot, npart, nkey)
            else:
                # query metric: dot
                dot = ht.mul_op(query_lookups, key_mat)
                resp = ht.reduce_sum_op(dot, axes=[3])
                # (bs * slot, npart, nkey)
            resp = self.bn_layer(resp)
            codes = ht.argmax_op(resp, 2)
            self.codebook_update = ht.sparse_set_op(self.codebooks, x, codes)
            # (bs * slot, npart)
            if self.mode == 'vq':
                if not self.share_weights:
                    codes = ht.add_op(codes, self.dbase)
                outputs = ht.embedding_lookup_op(self.value_matrix, codes)
                # (bs * slot, npart, pdim)
                outputs_final = ht.add_op(ht.stop_gradient_op(
                    ht.minus_op(outputs, inputs)), inputs)
                reg = ht.minus_op(outputs, ht.stop_gradient_op(inputs))
                reg = ht.power_op(reg, 2)
                self.reg = ht.reduce_mean_op(reg, axes=(0, 1, 2))
            else:
                resp_prob = ht.softmax_op(resp)
                # (bs * slot, npart, nkey)
                nb_idxs_onehot = ht.one_hot_op(codes, self.num_choices)
                # (bs * slot, npart, nkey)
                nb_idxs_onehot = ht.minus_op(resp_prob, ht.stop_gradient_op(
                    ht.minus_op(resp_prob, nb_idxs_onehot)))
                if self.share_weights:
                    outputs = ht.matmul_op(
                        # (bs * slot * npart, nkey)
                        ht.array_reshape_op(
                            nb_idxs_onehot, (-1, self.num_choices)),
                        self.value_matrix)  # (nkey, pdim)
                    # (bs * slot * npart, pdim)
                else:
                    outputs = ht.batch_matmul_op(
                        # (npart, bs * slot, nkey)
                        ht.transpose_op(nb_idxs_onehot, [1, 0, 2]),
                        ht.array_reshape_op(self.value_matrix, (-1, self.num_choices, self.part_embedding_dim)))  # (npart, nkey, pdim)
                    # (npart, bs * slot, pdim)
                    outputs = ht.transpose_op(outputs, [1, 0, 2])
                    # (bs * slot, npart, pdim)
                outputs_final = ht.array_reshape_op(
                    outputs, (-1, self.embedding_dim))
                # (bs * slot, dim)

            return outputs_final

    def make_inference(self, x):
        with ht.context(self.ctx):
            codes = ht.embedding_lookup_op(self.codebooks, x)
            # (bs, slot, npart)
            if not self.share_weights:
                codes = ht.add_op(codes, ht.array_reshape_op(
                    self.dbase, (-1, self.num_slot, self.num_parts)))
            outputs = ht.embedding_lookup_op(self.value_matrix, codes)
            # (bs, slot, npart, pdim)
            outputs = ht.array_reshape_op(outputs, (-1, self.embedding_dim))
            # (bs * slot, dim)
            return outputs


class AutoDimEmbedding(BaseLayer):
    def __init__(self, num_embeddings, dim_candidates, num_slot, batch_size, log_alpha=False, initializer=ht.init.GenXavierNormal(), name='embedding', ctx=None):
        from .normalization import BatchNorm
        self.num_embeddings = num_embeddings
        self.num_slot = num_slot
        self.batch_size = batch_size
        self.dim_candidates = dim_candidates
        self.dim_candidates.sort()
        self.num_cands = len(dim_candidates)
        self.max_dim = self.dim_candidates[-1]
        self.name = name
        self.ctx = ctx
        self.bn_layers = {dim: BatchNorm(self.max_dim, scale=False, bias=False, name='bn{}'.format(
            dim)) for dim in self.dim_candidates}
        self.embedding_tables = {dim: initializer(shape=(num_embeddings, dim), name='{}{}'.format(
            name, dim), ctx=self.ctx) for dim in dim_candidates}
        self.weights = {dim: initializer(shape=(num_slot, dim, self.max_dim), name='weight{}'.format(
            dim), ctx=self.ctx)for dim in dim_candidates}
        self.biases = {dim: initializer(shape=(num_slot, 1, self.max_dim,), name='bias{}'.format(
            dim), ctx=self.ctx)for dim in dim_candidates}
        self.alpha = initializer(
            shape=(num_slot, self.num_cands), name='alphas', trainable=False, ctx=self.ctx)
        self.use_log_alpha = log_alpha
        if log_alpha:
            uniform_noise = np.random.uniform(0, 1, size=(self.num_cands,))
            gnoise = -np.log(-np.log(uniform_noise))
            self.gnoise = ht.Variable(
                'gumbel_noises', value=gnoise, trainable=False, ctx=self.ctx)

    def __call__(self, x):
        lookups = {}
        for dim in self.dim_candidates:
            cur_x = ht.embedding_lookup_op(self.embedding_tables[dim], x)
            lookups[dim] = cur_x
            # (bs, nslot, cdim)
        self.lookups = lookups
        return self.make_embed(lookups)

    def make_embed(self, lookups):
        middle_results = []
        for dim, lookup in zip(self.dim_candidates, lookups.values()):
            # (bs, nslot, cdim)
            cur_x = ht.transpose_op(lookup, (1, 0, 2))
            # (nslot, bs, cdim)
            cur_x = ht.batch_matmul_op(cur_x, self.weights[dim])
            # (nslot, bs, dim)
            cur_bias = ht.broadcastto_op(self.biases[dim], cur_x)
            cur_x = ht.add_op(cur_x, cur_bias)
            # (nslot, bs, dim)
            cur_x = ht.transpose_op(cur_x, (1, 0, 2))
            # (bs, nslot, dim)
            cur_x = ht.array_reshape_op(cur_x, (-1, self.max_dim))
            # (bs * nslot, dim)
            cur_x = self.bn_layers[dim](cur_x)
            cur_x = ht.array_reshape_op(
                cur_x, (-1, self.num_slot, self.max_dim, 1))
            # (bs, nslot, dim, 1)
            middle_results.append(cur_x)
        if self.use_log_alpha:
            log_alpha = ht.log_op(self.alpha)
            w_noise = ht.add_op(
                log_alpha, ht.broadcastto_op(self.gnoise, log_alpha))
        else:
            w_noise = self.alpha
        w_noise = ht.mul_byconst_op(
            w_noise, 1, const_updater=lambda t: (1 / max(0.01, 1-0.00005*t)))
        p_weight = ht.softmax_op(w_noise)
        # (nslot, ncands)
        p_weight = ht.array_reshape_op(
            p_weight, (1, self.num_slot, self.num_cands, 1))
        p_weight = ht.broadcast_shape_op(
            p_weight, (self.batch_size, self.num_slot, self.num_cands, 1))
        # (bs, nslot, ncands, 1)
        sparse_inputs = ht.concatenate_op(middle_results, axis=3)
        # (bs, nslot, dim, ncands)
        final_embedding = ht.batch_matmul_op(sparse_inputs, p_weight)
        # (bs, nslot, dim, 1)
        return final_embedding

    def get_arch_params(self, var2arr):
        copy_params = {}
        ori_params = {}
        for node, arr in var2arr.items():
            if node.trainable and not node.is_embed:
                copy_params[node] = ht.empty(arr.shape, ctx=arr.ctx)
                ori_params[node] = arr
        for dim, lookup in self.lookups.items():
            copy_params[lookup] = ht.empty(
                (self.batch_size, self.num_slot, dim), ctx=self.ctx)
        self.copy_params = copy_params
        self.ori_params = ori_params
        self.workspace = ht.empty((len(self.copy_params),), ctx=self.ctx)
        self.norm = ht.empty((1,), ctx=self.ctx)
        self.dalpha_values = [ht.empty(
            (self.num_slot, self.num_cands), ctx=self.ctx) for _ in range(2)]

    def make_subexecutors(self, model, dense_input, y_, prediction, loss, opt):
        from ..optimizer import OptimizerOp
        new_subexe = {'validate': [loss, prediction, y_]}

        # explicitly minimize
        opt.loss = loss
        var_list = opt.get_var_list(loss)
        opt.params = var_list
        all_var_list = [self.alpha] + var_list
        grads, opt.backward2forward, opt.forward2backward = ht.gradients(
            loss, all_var_list, return_all=True)
        optimizer_node = OptimizerOp(grads[1:], opt)
        new_subexe['train'] = [loss, prediction, y_, optimizer_node]
        new_subexe['all_no_update'] = grads + list(self.lookups.values())

        self.var_lookups = {dim: ht.placeholder_op(
            'lookups', value=np.zeros((self.batch_size, self.num_slot, dim)), trainable=False, ctx=self.ctx) for dim in self.dim_candidates}
        new_loss, new_pred = model(self.make_embed(
            self.var_lookups), dense_input, y_)
        alpha_grad = ht.gradients(new_loss, [self.alpha])
        new_subexe['alpha'] = alpha_grad

        return new_subexe

    def copy_from(self, stream):
        for node, arr in self.ori_params.items():
            self.copy_params[node]._async_copyfrom(arr, stream)

    def copy_from_lookups(self, lookups, stream):
        for node, value in zip(self.lookups.values(), lookups):
            self.copy_params[node]._async_copyfrom(value, stream)

    def copy_to(self, var2arr, stream):
        for node, arr in self.ori_params.items():
            arr._async_copyfrom(self.copy_params[node], stream)
        for dim, node in self.lookups.items():
            var2arr[self.var_lookups[dim]]._async_copyfrom(
                self.copy_params[node], stream)

    def train(self, executor, lr, r=1e-2):
        from ..gpu_links import all_fro_norm, matrix_elementwise_divide_const, all_add_, div_n_mul_, matrix_elementwise_minus, matrix_elementwise_add_simple, sgd_update
        var2arr = executor.config.placeholder_to_arr_map
        stream = executor.config.comp_stream
        self.copy_from(stream)
        executor.run('train', dataloader_step=False)  # train data
        all_grads = executor.run('all_no_update')  # valid data
        dalpha = all_grads[0]
        self.dalpha_values[0]._async_copyfrom(dalpha, stream)
        others = all_grads[1:-self.num_cands]
        self.copy_from_lookups(all_grads[-self.num_cands:], stream)
        all_fro_norm(others, self.workspace, self.norm, stream)
        self.copy_to(var2arr, stream)
        matrix_elementwise_divide_const(r, self.norm, self.norm, stream)
        # for embedding, only add for train embedding
        tensors = [var2arr[x] for x in self.var_lookups.values()] + \
            list(self.ori_params.values())
        all_add_(tensors, others, self.norm, stream=stream)
        gradp = executor.run('alpha', dataloader_step=False)  # train data
        self.dalpha_values[1]._async_copyfrom(gradp[0], stream)
        # for embedding, only add for train embedding
        all_add_(tensors, others, self.norm, -2, stream=stream)
        gradn = executor.run('alpha', dataloader_step=False)  # train data
        # for embedding, only add for train embedding
        all_add_(tensors, others, self.norm, stream=stream)
        matrix_elementwise_minus(
            self.dalpha_values[1], gradn[0], self.dalpha_values[1], stream)
        div_n_mul_(self.dalpha_values[1], self.norm, -lr / 2, stream)
        matrix_elementwise_add_simple(
            self.dalpha_values[0], self.dalpha_values[1], self.dalpha_values[0], stream)
        sgd_update(var2arr[self.alpha], self.dalpha_values[0], lr, 0, stream)
        results = executor.run(
            'train', convert_to_numpy_ret_vals=True)  # train data
        return results

    def make_retrain(self, xs, separate_num_embeds, var2arr, stream):
        from ..gpu_links import argmax
        for node, value in var2arr.items():
            node.tensor_value = value
        dim_choice = ht.empty((self.num_slot, ), ctx=self.ctx)
        argmax(self.alpha.tensor_value, dim_choice, 1, stream=stream)
        stream.sync()
        dim_choice = [self.dim_candidates[int(ind)]
                      for ind in dim_choice.asnumpy()]
        new_embedding_tables = []
        new_weights = []
        new_biases = []
        cur_offset = 0
        for i, (nembed, dim) in enumerate(zip(separate_num_embeds, dim_choice)):
            cur_embed_table = ht.empty((nembed, dim), ctx=self.ctx)
            cur_embed_table._async_copyfrom_offset(
                self.embedding_tables[dim].tensor_value, stream, cur_offset * dim, 0, nembed * dim)
            new_embedding_tables.append(ht.placeholder_op(
                'new_embed_{}'.format(i), value=cur_embed_table, ctx=self.ctx))
            cur_weight = ht.empty((dim, self.max_dim), ctx=self.ctx)
            cur_weight._async_copyfrom_offset(
                self.weights[dim].tensor_value, stream, i * dim * self.max_dim, 0, dim * self.max_dim)
            new_weights.append(ht.placeholder_op(
                'new_weight_{}'.format(i), value=cur_weight))
            cur_bias = ht.empty((self.max_dim, ), ctx=self.ctx)
            cur_bias._async_copyfrom_offset(
                self.biases[dim].tensor_value, stream, i * self.max_dim, 0, self.max_dim)
            new_biases.append(ht.placeholder_op(
                'new_bias_{}'.format(i), value=cur_bias))
            cur_offset += nembed
        stream.sync()
        self.embedding_tables = new_embedding_tables
        self.weights = new_weights
        self.biases = new_biases
        all_lookups = []
        for x, table, weight, bias in zip(xs, self.embedding_tables, self.weights, self.biases):
            lookups = ht.embedding_lookup_op(table, x)
            # (bs, cdim)
            lookups = ht.linear_op(lookups, weight, bias)
            # (bs, dim)
            all_lookups.append(lookups)
        all_lookups = ht.concatenate_op(all_lookups, 1)
        return all_lookups