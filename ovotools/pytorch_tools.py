import torch
import numpy as np

class DummyTimer:
    '''
    replacement for IgniteTimer if it is not provided
    '''

    class TimerWatch:
        def __init__(self, timer, name): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def __init__(self): pass
    def start(self, name): pass
    def end(self, name): pass
    def watch(self, name): return self.TimerWatch(self, name)


class MarginBaseLoss:
    '''
    L2-constrained Softmax Loss for Discriminative Face Verification https://arxiv.org/pdf/1703.09507
    margin based loss with distance weighted sampling https://arxiv.org/pdf/1706.07567.pdf
    '''
    ignore_index = -100
    def __init__(self, model, classes, device, params):
        assert params.data.samples_per_class >= 2
        self.model = model
        self.device = device
        self.params = params
        self.classes = sorted(classes)
        self.classes_dict = {v: i for i, v in enumerate(self.classes)}
        self.lambda_rev = 1/params.distance_weighted_sampling.lambda_
        self.timer = DummyTimer()
        print('classes: ', len(self.classes))

    def set_timer(self, timer):
        self.timer = timer

    def classes_to_ids(self, y_class, ignore_index = -100):
        return torch.tensor([self.classes_dict.get(int(c.item()), ignore_index) for c in y_class]).to(self.device)

    def l2_loss(self, net_output, y_class):
        with self.timer.watch('time.l2_loss'):
            pred_class = net_output[0]
            class_nos = self.classes_to_ids(y_class, ignore_index=self.ignore_index)
            self.l2_loss_val = torch.nn.CrossEntropyLoss(ignore_index=self.ignore_index)(pred_class, class_nos)
            return self.l2_loss_val

    def last_l2_loss(self, net_output, y_class):
        return self.l2_loss_val

    def mb_loss(self, net_output, y_class):
        with self.timer.watch('time.mb_loss'):
            pred_embeddings = net_output[1]
            loss = 0
            n = len(pred_embeddings) # samples in batch
            dim =  pred_embeddings[0].shape[0] # dimensionality
            self.true_pos = 0
            self.true_neg = 0
            self.false_pos = 0
            self.false_neg = 0

            with self.timer.watch('time.d_ij'):
                assert len(pred_embeddings.shape) == 2, pred_embeddings.shape
                norm = (pred_embeddings ** 2).sum(1)
                self.d_ij = norm.view(-1, 1) + norm.view(1, -1) - 2.0 * torch.mm(pred_embeddings, torch.transpose(pred_embeddings, 0, 1)) #https://discuss.pytorch.org/t/efficient-distance-matrix-computation/9065/8
                self.d_ij = torch.sqrt(torch.clamp(self.d_ij, min=0.0) + 1.0e-8)

            for i_start in range(0, n, self.params.data.samples_per_class): # start of class block
                i_end = i_start + self.params.data.samples_per_class # start of class block
                for i in range(i_start, i_end):
                    d = self.d_ij[i,:].detach()
                    prob = torch.exp(-(d - 1.4142135623730951)**2 * dim) #https://arxiv.org/pdf/1706.07567.pdf
                    weights = (1/prob.clamp(min = self.lambda_rev)).cpu().numpy()
                    weights[i] = 0 # dont join with itself
                    # select positive pair
                    weights_same = weights[i_start: i_end] # i-th element already excluded
                    j = np.random.choice(range(i_start, i_end), p = weights_same/np.sum(weights_same), replace=False)
                    assert j != i
                    loss += (self.model.mb_loss_alpha + (self.d_ij[i,j] - self.model.mb_loss_beta)).clamp(min=0)  #https://arxiv.org/pdf/1706.07567.pdf
                    # select neg. pait
                    weights = np.delete(weights, np.s_[i_start: i_end], axis=0)
                    k = np.random.choice(range(0, n - self.params.data.samples_per_class), p = weights/np.sum(weights), replace=False)
                    if k >= i_start:
                        k += self.params.data.samples_per_class
                    loss += (self.model.mb_loss_alpha - (self.d_ij[i,k] - self.model.mb_loss_beta)).clamp(min=0)  #https://arxiv.org/pdf/1706.07567.pdf
                    self.mb_loss_val = loss[0] / len(pred_embeddings)
                    negative = (d > self.model.mb_loss_beta.detach()).float()
                    positive = (d <= self.model.mb_loss_beta.detach()).float()
                    fn = sum(negative[i_start: i_end])
                    self.false_neg += fn
                    tp = sum(positive[i_start: i_end])
                    self.true_pos += tp
                    fp = sum(positive[: i_start]) + sum(positive[i_end:])
                    self.false_pos += fp
                    fn = sum(negative[: i_start]) + sum(negative[i_end:])
                    self.true_neg += fn
            self.true_pos /= n
            self.true_neg /= n
            self.false_pos /= n
            self.false_neg /= n
            return self.mb_loss_val

    def last_mb_loss(self, net_output, y_class):
        return self.mb_loss_val

    def last_false_pos(self, net_output, y_class):
        return self.false_pos

    def last_false_neg(self, net_output, y_class):
        return self.false_neg

    def last_true_pos(self, net_output, y_class):
        return self.true_pos

    def last_true_neg(self, net_output, y_class):
        return self.true_neg

    def loss(self, net_output, y_class):
        self.loss_val = self.l2_loss(net_output, y_class) + self.mb_loss(net_output, y_class)
        return self.loss_val
