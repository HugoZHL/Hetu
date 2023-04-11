import hetu as ht
import numpy as np
from copy import copy


class HetuTester(object):
    def __init__(self, op, num_inputs, *args, in_dtype=None, **kargs):
        valid_type = ('f', 'i', 'ui', 'uf')
        if in_dtype is None:
            in_dtype = 'f'
        if in_dtype in valid_type:
            self.in_dtype = [in_dtype] * num_inputs
        else:
            assert all([dt in valid_type for dt in in_dtype])
            self.in_dtype = in_dtype
        self.node_dtype = [np.float32 if dt in (
            'f', 'uf') else np.int32 for dt in self.in_dtype]
        self.make_inputs(num_inputs)
        self.make_ops(op, *args, **kargs)
        self.make_executors()
        self.cpu_feeds = None
        self.gpu_feeds = None

    def make_inputs(self, num_inputs):
        self.cpu_inputs = [ht.Variable(name='input%d' % i, ctx=ht.cpu(), dtype=self.node_dtype[i])
                           for i in range(num_inputs)]
        self.gpu_inputs = [ht.Variable(name='input%d' % i, ctx=ht.gpu(0), dtype=self.node_dtype[i])
                           for i in range(num_inputs)]

    def make_ops(self, op, *args, **kargs):
        self.cpu_op = op(*self.cpu_inputs, *args, **kargs)
        self.gpu_op = op(*self.gpu_inputs, *args, **kargs)

    def make_executors(self):
        self.cpu_executor = ht.Executor([self.cpu_op], ctx=ht.cpu())
        self.gpu_executor = ht.Executor([self.gpu_op], ctx=ht.gpu(0))

    def random_float(self, shape, low=None, high=None):
        if low is not None:
            return np.random.uniform(low, high, size=shape).astype(np.float32)
        else:
            return np.random.normal(size=shape).astype(np.float32)

    def random_int(self, shape, low, high):
        return np.random.randint(low=low, high=high, size=shape).astype(np.int32)

    def run(self, input_vals):
        if self.cpu_feeds is None:
            self.cpu_feeds = self.cpu_inputs
        if self.gpu_feeds is None:
            self.gpu_feeds = self.gpu_inputs
        cpu_result = self.cpu_executor.run(
            feed_dict={k: v for k, v in zip(self.cpu_feeds, input_vals)}, convert_to_numpy_ret_vals=True)
        gpu_result = self.gpu_executor.run(
            feed_dict={k: v for k, v in zip(self.gpu_feeds, input_vals)}, convert_to_numpy_ret_vals=True)
        return cpu_result, gpu_result

    def test(self, input_shapes, rtol=1e-7, atol=0):
        input_vals = []
        for dt, shape in zip(self.in_dtype, input_shapes):
            if dt == 'f':
                cur_val = self.random_float(shape)
            elif dt == 'uf':
                cur_val = self.random_float(shape, 0, 10)
            elif dt == 'i':
                cur_val = self.random_int(shape, -10, 10)
            else:
                cur_val = self.random_int(shape, 1, 1000)
            input_vals.append(cur_val)
        cpu_result, gpu_result = self.run(input_vals)
        assert not np.any(np.logical_or(np.isnan(cpu_result), np.isinf(
            cpu_result))), 'NAN of INF exist in cpu result!'
        assert not np.any(np.logical_or(np.isnan(gpu_result), np.isinf(
            gpu_result))), 'NAN of INF exist in gpu result!'
        np.testing.assert_allclose(
            cpu_result[0], gpu_result[0], rtol=rtol, atol=atol)
        print('Op %s pass the test with shapes: %s' %
              (self.gpu_op, input_shapes))


class HetuOptimizerTester(HetuTester):
    def __init__(self, opt, input_shapes):
        assert isinstance(opt, ht.optim.Optimizer)
        opt.backward2forward = {}
        opt.forward2backward = {}
        opt.loss = None
        self.cpu_opt = opt
        self.gpu_opt = copy(opt)
        has_betats = (ht.optim.AdamOptimizer,
                      ht.optim.AdamWOptimizer, ht.optim.LambOptimizer)
        if isinstance(self.cpu_opt, has_betats):
            ctx = ht.cpu()
            self.cpu_opt.betatss = {ctx: ht.init.constant(
                (2,), 1.0, f'adam_betats_cpu', False, ctx)}
            self.cpu_opt.betats_update_ops = {ctx: ht.optim.betats_update_op(
                self.cpu_opt.betatss[ctx], self.cpu_opt.beta1, self.cpu_opt.beta2, ctx)}
        if isinstance(self.gpu_opt, has_betats):
            ctx = ht.gpu(0)
            self.gpu_opt.betatss = {ctx: ht.init.constant(
                (2,), 1.0, f'adam_betats_gpu', False, ctx)}
            self.gpu_opt.betats_update_ops = {ctx: ht.optim.betats_update_op(
                self.gpu_opt.betatss[ctx], self.gpu_opt.beta1, self.gpu_opt.beta2, ctx)}
        self.input_shapes = input_shapes
        ctensors = []
        cparams = []
        gtensors = []
        gparams = []
        for i, shape in enumerate(input_shapes):
            cur_value = self.random_float(shape)
            ctensors.append(ht.array(cur_value, ht.cpu()))
            gtensors.append(ht.array(cur_value, ht.gpu(0)))
            cparams.append(ht.Variable(
                'ctemp{}'.format(i), value=ctensors[-1], ctx=ht.cpu()))
            gparams.append(ht.Variable(
                'gtemp{}'.format(i), value=gtensors[-1], ctx=ht.gpu(0)))
            cparams[-1].on_cpu = gparams[-1].on_gpu = True
            cparams[-1].on_gpu = gparams[-1].on_cpu = False
        self.cparams = cparams
        self.gparams = gparams
        self.ctensors = ctensors
        self.gtensors = gtensors
        ind = self.make_inputs(input_shapes)

        input_vals = [self.random_float(shape) for shape in input_shapes]
        if ind < len(input_shapes):
            # test sparse update
            print('Enable sparse test.')
            input_vals.pop(ind)
            shape = input_shapes[ind]
            prefix = (128, 26)
            indices = self.random_int(prefix, 0, shape[0])
            values = self.random_float(prefix + (shape[1],))
            input_vals.insert(ind, values)
            input_vals.insert(ind, indices)
        self.input_vals = input_vals
        self.ind = ind
        self.make_ops()
        self.make_executors()

    def make_inputs(self, input_shapes):
        num_inputs = len(input_shapes)
        if num_inputs > 1:
            flag = False
            for ind in range(num_inputs):
                if len(input_shapes[ind]) == 2:
                    flag = True
                    break
            if not flag:
                ind = num_inputs
        else:
            ind = num_inputs
        self.cpu_inputs = []
        self.gpu_inputs = []
        self.cpu_feeds = []
        self.gpu_feeds = []
        for i in range(num_inputs):
            if i == ind:
                cpu_ind_op = ht.Variable(
                    name='cpu_indices', ctx=ht.cpu())
                cpu_val_op = ht.Variable(name='cpu_values', ctx=ht.cpu())
                cpu_lookup_op = ht.embedding_lookup_op(
                    self.cparams[ind], cpu_ind_op, ctx=ht.cpu())
                gpu_ind_op = ht.Variable(
                    name='gpu_indices', ctx=ht.gpu(0))
                gpu_val_op = ht.Variable(name='gpu_values', ctx=ht.gpu(0))
                gpu_lookup_op = ht.embedding_lookup_op(
                    self.gparams[ind], gpu_ind_op, ctx=ht.gpu(0))
                cpu_grad_wlookup = ht.embedding_lookup_gradient_with_lookup_op(
                    cpu_val_op, cpu_ind_op, cpu_lookup_op, input_shapes[i], ctx=ht.cpu())
                cpu_grad_dgrad = ht.embedding_lookup_gradient_dedupgrad_op(
                    cpu_grad_wlookup, cpu_val_op, ctx=ht.cpu())
                gpu_grad_wlookup = ht.embedding_lookup_gradient_with_lookup_op(
                    gpu_val_op, gpu_ind_op, gpu_lookup_op, input_shapes[i], ctx=ht.gpu(0))
                gpu_grad_dgrad = ht.embedding_lookup_gradient_dedupgrad_op(
                    gpu_grad_wlookup, gpu_val_op, ctx=ht.gpu(0))
                self.cpu_inputs.append(cpu_grad_dgrad)
                self.gpu_inputs.append(gpu_grad_dgrad)
                self.cpu_feeds.extend([cpu_ind_op, cpu_val_op])
                self.gpu_feeds.extend([gpu_ind_op, gpu_val_op])
            else:
                cpu_op = ht.Variable(name='input%d' % i, ctx=ht.cpu())
                gpu_op = ht.Variable(name='input%d' % i, ctx=ht.gpu(0))
                self.cpu_inputs.append(cpu_op)
                self.gpu_inputs.append(gpu_op)
                self.cpu_feeds.append(cpu_op)
                self.gpu_feeds.append(gpu_op)
        return ind

    def make_ops(self):
        self.cpu_op = [
            self.cpu_opt.opt_op_type(param, grad, self.cpu_opt)
            if grad.op_type == 'PlaceholderOp'
            else ht.assign_with_indexedslices_op(param, self.cpu_opt.sparse_opt_op_type(
                param, grad.inputs[0], grad, self.cpu_opt))
            for param, grad in zip(self.cparams, self.cpu_inputs)]
        self.gpu_op = [
            self.gpu_opt.opt_op_type(param, grad, self.gpu_opt)
            if grad.op_type == 'PlaceholderOp'
            else ht.assign_with_indexedslices_op(param, self.gpu_opt.sparse_opt_op_type(
                param, grad.inputs[0], grad, self.gpu_opt))
            for param, grad in zip(self.gparams, self.gpu_inputs)]

    def test(self, iters=5, rtol=1e-7, atol=0):
        sparse_atol = 5e-5
        for _ in range(iters):
            self.run(self.input_vals)
            self.gpu_executor.config.comp_stream.sync()
            for i, (ctensor, gtensor) in enumerate(zip(self.ctensors, self.gtensors)):
                cur_atol = atol
                if i == self.ind:
                    cur_atol = sparse_atol
                np.testing.assert_allclose(
                    ctensor.asnumpy(), gtensor.asnumpy(), rtol=rtol, atol=cur_atol)
            rtol *= 2
            atol *= 2
            sparse_atol *= 2
        print('Optimizer %s pass the test with shapes: %s' %
              (self.gpu_opt, self.input_shapes))
