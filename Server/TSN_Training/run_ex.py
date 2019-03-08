import argparse
import os
import time
import shutil
import torch
import torchvision
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
from torch.nn.utils import clip_grad_norm

from dataset import TSNDataSet
from models import TSN
from transforms import *

import cv2

# options
parser = argparse.ArgumentParser(
    description="Standard video-level testing")
parser.add_argument('dataset', type=str, choices=['ucf101', 'hmdb51', 'kinetics'])
parser.add_argument('modality', type=str, choices=['RGB', 'Flow', 'RGBDiff'])
parser.add_argument('test_list', type=str)
parser.add_argument('weights', type=str)
parser.add_argument('--arch', type=str, default="resnet101")
parser.add_argument('--save_scores', type=str, default=None)
parser.add_argument('--test_segments', type=int, default=3)
parser.add_argument('--max_num', type=int, default=-1)
parser.add_argument('--test_crops', type=int, default=1)
parser.add_argument('--input_size', type=int, default=224)
parser.add_argument('--crop_fusion_type', type=str, default='avg',
                    choices=['avg', 'max', 'topk'])
parser.add_argument('--k', type=int, default=3)
parser.add_argument('--dropout', type=float, default=0.7)
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--gpus', nargs='+', type=int, default=None)
parser.add_argument('--flow_prefix', type=str, default='')

args = parser.parse_args()

best_prec1 = 0
num_class = 0

def main():
    global args, best_prec1, num_class
    args = parser.parse_args()

    if args.dataset == 'ucf101':
        num_class = 101
    elif args.dataset == 'hmdb51':
        num_class = 51
    elif args.dataset == 'kinetics':
        num_class = 400
    else:
        raise ValueError('Unknown dataset '+args.dataset)
    
    '''
    model = TSN(num_class, args.num_segments, args.modality,
                base_model=args.arch,
                consensus_type=args.consensus_type, dropout=args.dropout, partial_bn=not args.no_partialbn)
    '''
    model = TSN(num_class, 3, args.modality,
                base_model=args.arch,
                consensus_type=args.crop_fusion_type, dropout=args.dropout)

    checkpoint = torch.load(args.weights)
    print("model epoch {} best prec@1: {}".format(checkpoint['epoch'], checkpoint['best_prec1']))

    base_dict = {'.'.join(k.split('.')[1:]): v for k,v in list(checkpoint['state_dict'].items())}
    model.load_state_dict(base_dict)

    crop_size = model.crop_size
    scale_size = model.scale_size
    input_mean = model.input_mean
    input_std = model.input_std
    policies = model.get_optim_policies()
    train_augmentation = model.get_augmentation()

    model = torch.nn.DataParallel(model, device_ids=args.gpus).cuda()

    cudnn.benchmark = True

    # Data loading code
    if args.modality != 'RGBDiff':
        normalize = GroupNormalize(input_mean, input_std)
    else:
        normalize = IdentityTransform()

    if args.modality == 'RGB':
        data_length = 1
    elif args.modality in ['Flow', 'RGBDiff']:
        data_length = 5

    if args.test_crops == 1:
        cropping = torchvision.transforms.Compose([
            GroupScale(int(scale_size)),
            GroupCenterCrop(crop_size),
        ])
    elif args.test_crops == 10:
        cropping = torchvision.transforms.Compose([
            GroupOverSample(crop_size, scale_size)
        ])
    else:
        raise ValueError("Only 1 and 10 crops are supported while we got {}".format(args.test_crops))

    val_loader = torch.utils.data.DataLoader(
        TSNDataSet("", args.test_list, num_segments=args.test_segments,
                   new_length=data_length,
                   modality=args.modality,
                   image_tmpl="img_{:05d}.jpg" if args.modality in ["RGB", "RGBDiff"] else args.flow_prefix+"{}_{:05d}.jpg",
                   test_mode=True,
                   transform=torchvision.transforms.Compose([
                       cropping,
                       Stack(roll=args.arch == 'BNInception'),
                       ToTorchFormatTensor(div=args.arch != 'BNInception'),
                       normalize
                   ])),
        batch_size=1, shuffle=False,
        num_workers=0, pin_memory=True)


    
    validate(val_loader, model, 0)




def validate(val_loader, model, iter, logger=None):
    batch_time = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    with open(args.test_list, 'r') as fp:
        lines = fp.readlines()

    dict = {0:'None', 1:'ApplyEyeMakeup'}
    with open('classInd.txt','r') as fp:
        lines2 = fp.readlines()
    for i in range(101):
        if i == 0:
            continue
        dict[int(lines2[i].split(' ')[0])] = lines2[i].split(' ')[1].replace('\n', '')

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, (input, target) in enumerate(val_loader):
        # print(input.shape)            # torch.Size([1, 9, 224, 224])  第一個數字是batch_size
        # print(target)                 # tensor([1])
        tar_val = int(target[0])

        target = target.cuda(async=True)
        input_var = torch.autograd.Variable(input, volatile=True)
        target_var = torch.autograd.Variable(target, volatile=True)

        # compute output
        output = model(input_var)
        # print(output.shape)           # torch.Size([batch_size, 2])

        topk = (1,5)
        maxk = max(topk)
        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        if tar_val == 1:
            print(output)
            print(pred, tar_val)
        top1_val = int(pred[0][0])

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1,5))
        top1.update(prec1[0], input.size(0))
        top5.update(prec5[0], input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        print(i)

        if i % 10 == 0:
            print(('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Prec@1 ({top1.avg:.3f})\t'
                  'Prec@5 ({top5.avg:.3f})'.format(
                   i, len(val_loader), batch_time=batch_time,
                   top1=top1, top5=top5)))

        # error_flag = 0
        # isBreak = 0
        # for j in range(3):
        #     tick = float(lines[i].split(' ')[1]) / 3.0
        #     my_index = int(tick/2.0+tick*j)
        #     img_s = lines[i].split(' ')[0] + '\\' + 'img_{:05d}.jpg'.format(my_index)
        #     img = cv2.imread(img_s)
        #     if tar_val != top1_val:
        #         cv2.putText(img, "False", (100, 220),  cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        #         error_flag = 1
        #     cv2.putText(img, dict[top1_val], (60, 30),  cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 1)
        #     cv2.imshow('test', img)
        #     if cv2.waitKey(100) == 27:
        #         isBreak = 1
        #         break

        # if error_flag == 1:
        #     if isBreak == 1:
        #         break
        #     if cv2.waitKey(500) == 27:
        #         break
        # elif isBreak == 1:
        #     break
        


    return top1.avg

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    pred[pred == 0] = num_class
    
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

if __name__ == '__main__':
    main()
