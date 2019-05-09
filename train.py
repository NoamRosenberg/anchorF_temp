import time
import os
import copy
import argparse
import pdb
import collections
import sys

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.autograd import Variable
from torchvision import datasets, models, transforms
import torchvision
import tensorflow as tf
import model
from anchors import Anchors
import losses
from dataloader import CocoDataset, CSVDataset, collater, Resizer, AspectRatioBasedSampler, Augmenter, UnNormalizer, Normalizer
from torch.utils.data import Dataset, DataLoader

import coco_eval
import csv_eval

assert torch.__version__.split('.')[1] == '4'

print('CUDA available: {}'.format(torch.cuda.is_available()))


def main(args=None):

	parser     = argparse.ArgumentParser(description='Simple training script for training a RetinaNet network.')

	parser.add_argument('--dataset', help='Dataset type, must be one of csv or coco.', default='coco')
	parser.add_argument('--coco_path', help='Path to COCO directory', default='/data/deeplearning/dataset/coco2017')
	parser.add_argument('--csv_train', help='Path to file containing training annotations (see readme)')
	parser.add_argument('--csv_classes', help='Path to file containing class list (see readme)')
	parser.add_argument('--csv_val', help='Path to file containing validation annotations (optional, see readme)')

	parser.add_argument('--depth', help='Resnet depth, must be one of 18, 34, 50, 101, 152', type=int, default=50)
	parser.add_argument('--epochs', help='Number of epochs', type=int, default=100)
	parser.add_argument('--lr', help='learning rate', type=float, default=1e-5)
	parser.add_argument('--s_norm', help='normalize regression outputs', type=float, default=4.0)
	parser.add_argument('--t_val', help='sensitivity of per pyramid loss', type=float, default=1.0)
	parser.add_argument('--IOU', help='IoU loss or regular regression loss', type=int, default=1)
	parser.add_argument('--rest_norm', help='weight for rest region, i.e. not effective region', type=float, default=1.0)
	parser.add_argument('--center', help='center the per pyramid value', type=int, default=0)
	parser.add_argument('--adam', help='adam opt', type=int, default=0)
	parser.add_argument('--resume', help='path to model', type=str, default='/data/deeplearning/dataset/training/data/newLossRes/coco_retinanet_16_snorm_4.0_tval_1.0_restnorm_1.0_IOU_1.pt')
	parser.add_argument('--save_model_dir', default='/data/deeplearning/dataset/training/data/newLossRes')
	parser.add_argument('--log_dir', default='/data/deeplearning/dataset/training/data/log_dir')
	parser = parser.parse_args(args)
	tf.summary.FileWriter(parser.log_dir)
	# Create the data loaders
	if parser.dataset == 'coco':
		if parser.coco_path is None:
			raise ValueError('Must provide --coco_path when training on COCO,')

		dataset_train = CocoDataset(parser.coco_path, set_name='train2017', transform=transforms.Compose([Normalizer(), Augmenter(), Resizer()]))
		dataset_val = CocoDataset(parser.coco_path, set_name='val2017', transform=transforms.Compose([Normalizer(), Resizer()]))

	elif parser.dataset == 'csv':
		if parser.csv_train is None:
			raise ValueError('Must provide --csv_train when training on COCO,')
		if parser.csv_classes is None:
			raise ValueError('Must provide --csv_classes when training on COCO,')
		dataset_train = CSVDataset(train_file=parser.csv_train, class_list=parser.csv_classes, transform=transforms.Compose([Normalizer(), Augmenter(), Resizer()]))
		if parser.csv_val is None:
			dataset_val = None
			print('No validation annotations provided.')
		else:
			dataset_val = CSVDataset(train_file=parser.csv_val, class_list=parser.csv_classes, transform=transforms.Compose([Normalizer(), Resizer()]))

	else:
		raise ValueError('Dataset type not understood (must be csv or coco), exiting.')

	sampler = AspectRatioBasedSampler(dataset_train, batch_size=2, drop_last=False)
	dataloader_train = DataLoader(dataset_train, num_workers=0, collate_fn=collater, batch_sampler=sampler)

	if dataset_val is not None:
		sampler_val = AspectRatioBasedSampler(dataset_val, batch_size=1, drop_last=False)
		dataloader_val = DataLoader(dataset_val, num_workers=0, collate_fn=collater, batch_sampler=sampler_val)

	if parser.resume is not None:
		retinanet = torch.load(parser.resume)
		start_epoch = int(parser.resume.split('coco_retinanet_')[1].split('_')[0])
	else:
		start_epoch = 0
		# Create the model
		if parser.depth == 18:
			retinanet = model.resnet18(num_classes=dataset_train.num_classes(), pretrained=True)
		elif parser.depth == 34:
			retinanet = model.resnet34(num_classes=dataset_train.num_classes(), pretrained=True)
		elif parser.depth == 50:
			retinanet = model.resnet50(num_classes=dataset_train.num_classes(), pretrained=True)
		elif parser.depth == 101:
			retinanet = model.resnet101(num_classes=dataset_train.num_classes(), pretrained=True)
		elif parser.depth == 152:
			retinanet = model.resnet152(num_classes=dataset_train.num_classes(), pretrained=True)
		else:
			raise ValueError('Unsupported model depth, must be one of 18, 34, 50, 101, 152')

	use_gpu = True

	if use_gpu:
		retinanet = retinanet.cuda()
	
	retinanet = torch.nn.DataParallel(retinanet).cuda()

	retinanet.training = True
	if parser.adam:
		optimizer = optim.Adam(retinanet.parameters(), lr=parser.lr)
	else:
		optimizer = optim.SGD(retinanet.parameters(), lr=parser.lr, momentum=0.9, weight_decay=0.0001)
		scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3, verbose=True)

	loss_hist = collections.deque(maxlen=500)

	retinanet.train()
	retinanet.module.freeze_bn()

	print('Num training images: {}'.format(len(dataset_train)))


	for epoch_num in range(start_epoch, parser.epochs):

		retinanet.train()
		retinanet.module.freeze_bn()
		
		epoch_loss = []

		for iter_num, data in enumerate(dataloader_train):
			try:
				iter_loss = []
				optimizer.zero_grad()

				per_picture_loss, follow_ = retinanet([data['img'].cuda().float(), data['annot']], parser)


				batch_loss = per_picture_loss.mean()
				
				if bool(batch_loss == 0):
					continue

				batch_loss.backward()

				torch.nn.utils.clip_grad_norm_(retinanet.parameters(), 0.1)

				optimizer.step()
				if iter_num > 100:
					loss_hist.append(float(batch_loss))

				epoch_loss.append(float(batch_loss))
				iter_loss.append(float(batch_loss))
				#example of pyramid losses
				instance_loss = np.prod(follow_[0])
				tf.summary.scalar('mean_iter_loss', np.mean(iter_loss))
				if iter_num % 10 == 0:
					print('Epoch: {} | Iteration: {} | Loss: {:1.5f} | Running loss: {:1.5f} | py_losses: {} | prod: {}'
						  .format(epoch_num, iter_num, np.mean(iter_loss), np.mean(loss_hist), follow_[0], round(instance_loss,3)))
				del batch_loss
			except Exception as e:
				print(e)
				continue

#		if parser.dataset == 'coco':
#
#			print('Evaluating dataset')
#			coco_eval.evaluate_coco(dataset_val, retinanet, parser)
#
#		elif parser.dataset == 'csv' and parser.csv_val is not None:
#			print('Evaluating dataset')
#			mAP = csv_eval.evaluate(dataset_val, retinanet)

		
		scheduler.step(np.mean(epoch_loss))	
		print('saving checkpoint')
		torch.save(retinanet.module, os.path.join(parser.save_model_dir,'{}_retinanet_{}_snorm_{}_tval_{}_restnorm_{}_lr_{}_ada_{}.pt'.format(parser.dataset, epoch_num, parser.s_norm, parser.t_val, parser.rest_norm, parser.lr, parser.adam)))

	retinanet.eval()
	print('saving model')
	torch.save(retinanet, os.path.join(parser.save_model_dir,'model_final_{}_snorm_{}_tval_{}_restnorm_{}_lr_{}_ada_{}.pt'.format(epoch_num, parser.s_norm, parser.t_val, parser.rest_norm, parser.lr, parser.adam)))

if __name__ == '__main__':
 main()
