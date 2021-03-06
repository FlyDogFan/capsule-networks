"""
Dynamic Routing Between Capsules
https://arxiv.org/abs/1710.09829

PyTorch implementation by Kenta Iwasaki @ Gram.AI.
"""

import torch
import torch.nn.functional as F
from torch import nn


def softmax(input, dim=1):
    input_size = input.size()

    trans_input = input.transpose(dim, len(input_size) - 1)
    trans_size = trans_input.size()

    input_2d = trans_input.contiguous().view(-1, trans_size[-1])

    soft_max_2d = F.softmax(input_2d)

    soft_max_nd = soft_max_2d.view(*trans_size)
    return soft_max_nd.transpose(dim, len(input_size) - 1)


class CapsuleLayer(nn.Module):
    def __init__(self, num_capsules, num_route_nodes, in_channels, out_channels, kernel_size=None, stride=None,
                 num_iterations=3):
        super(CapsuleLayer, self).__init__()

        self.num_route_nodes = num_route_nodes
        self.num_iterations = num_iterations

        self.num_capsules = num_capsules

        if num_route_nodes != -1:
            self.route_logits = nn.Parameter(torch.zeros(num_capsules, 1, num_route_nodes, 1, 1))
            self.route_weights = nn.Parameter(torch.randn(num_capsules, num_route_nodes, in_channels, out_channels))
        else:
            self.capsules = nn.ModuleList(
                [nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, padding=0) for _ in
                 range(num_capsules)])

    def squash(self, tensor, dim=-1):
        squared_norm = (tensor ** 2).sum(dim=dim, keepdim=True)
        scale = squared_norm / (1 + squared_norm)
        return scale * tensor / torch.sqrt(squared_norm)

    def forward(self, x):
        if self.num_route_nodes != -1:
            priors = x[None, :, :, None, :] @ self.route_weights[:, None, :, :, :]

            logits = self.route_logits
            for i in range(self.num_iterations):
                probs = softmax(logits, dim=2)
                outputs = self.squash((probs * priors).sum(dim=2, keepdim=True))

                if i != self.num_iterations - 1:
                    delta_logits = (priors * outputs).sum(dim=-1, keepdim=True)
                    logits = logits + delta_logits
        else:
            outputs = [capsule(x).view(x.size(0), -1, 1) for capsule in self.capsules]
            outputs = torch.cat(outputs, dim=-1)
            outputs = self.squash(outputs)

        return outputs


class CapsuleNet(nn.Module):
    def __init__(self):
        super(CapsuleNet, self).__init__()

        self.conv1 = nn.Conv2d(in_channels=1, out_channels=256, kernel_size=9, stride=1)
        self.primary_capsules = CapsuleLayer(num_capsules=8, num_route_nodes=-1, in_channels=256, out_channels=32,
                                             kernel_size=9, stride=2)
        self.digit_capsules = CapsuleLayer(num_capsules=10, num_route_nodes=32 * 6 * 6, in_channels=8, out_channels=16)

        self.decoder = nn.Sequential(
            nn.Linear(16, 512),
            nn.ReLU(inplace=True),
            nn.Linear(512, 1024),
            nn.ReLU(inplace=True),
            nn.Linear(1024, 784),
            nn.Sigmoid()
        )

    def forward(self, x):
        x = F.relu(self.conv1(x), inplace=True)
        x = self.primary_capsules(x)
        x = self.digit_capsules(x).squeeze().transpose(0, 1)

        classes = (x ** 2).sum(dim=-1) ** 0.5
        classes = F.softmax(classes)

        # In all batches, get the most active capsule.
        _, max_length_indices = classes.max(dim=1)
        reconstructions = self.decoder(x[:, max_length_indices.data[0], :])

        return classes, reconstructions


class CapsuleLoss(nn.Module):
    def __init__(self):
        super(CapsuleLoss, self).__init__()
        self.reconstruction_loss = nn.MSELoss(size_average=True)

    def forward(self, images, labels, classes, reconstructions):
        left = torch.clamp(0.9 - classes, min=0) ** 2
        right = torch.clamp(classes - 0.1, min=0) ** 2

        margin_loss = labels * left + 0.5 * (1 - labels) * right
        margin_loss = margin_loss.sum(dim=1).mean()

        reconstruction_loss = self.reconstruction_loss(reconstructions, images)
        return margin_loss + 0.0005 * reconstruction_loss


if __name__ == "__main__":
    from torch.autograd import Variable
    from torch.optim import Adam
    from torchnet.engine import Engine
    from torchvision.datasets.mnist import MNIST
    from tqdm import tqdm
    import torchnet as tnt

    model = CapsuleNet()
    model.cuda()

    optimizer = Adam(model.parameters())

    engine = Engine()
    meter_loss = tnt.meter.AverageValueMeter()
    class_error = tnt.meter.ClassErrorMeter(accuracy=True)

    capsule_loss = CapsuleLoss()

    def get_iterator(mode):
        dataset = MNIST(root='./data', download=True, train=mode)
        data = getattr(dataset, 'train_data' if mode else 'test_data')
        labels = getattr(dataset, 'train_labels' if mode else 'test_labels')
        tensor_dataset = tnt.dataset.TensorDataset([data, labels])

        return tensor_dataset.parallel(batch_size=100, num_workers=4, shuffle=mode)

    def h(sample):
        data = sample[0].unsqueeze(1).float() / 255.0
        labels = torch.LongTensor(sample[1])

        labels = torch.sparse.torch.eye(10).index_select(dim=0, index=labels)

        data = Variable(data).cuda()
        labels = Variable(labels).cuda()

        classes, reconstructions = model(data)

        loss = capsule_loss(data, labels, classes, reconstructions)

        return loss, classes


    def reset_meters():
        class_error.reset()
        meter_loss.reset()


    def on_sample(state):
        state['sample'].append(state['train'])


    def on_forward(state):
        class_error.add(state['output'].data, torch.LongTensor(state['sample'][1]))
        meter_loss.add(state['loss'].data[0])


    def on_start_epoch(state):
        reset_meters()
        state['iterator'] = tqdm(state['iterator'])


    def on_end_epoch(state):
        print('Training loss: %.4f, accuracy: %.2f%%' % (meter_loss.value()[0], class_error.value()[0]))

        reset_meters()
        engine.test(h, get_iterator(False))
        print('Testing loss: %.4f, accuracy: %.2f%%' % (meter_loss.value()[0], class_error.value()[0]))


    engine.hooks['on_sample'] = on_sample
    engine.hooks['on_forward'] = on_forward
    engine.hooks['on_start_epoch'] = on_start_epoch
    engine.hooks['on_end_epoch'] = on_end_epoch
    engine.train(h, get_iterator(True), maxepoch=30, optimizer=optimizer)
