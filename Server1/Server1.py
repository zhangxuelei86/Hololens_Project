import numpy as np
import socket
import time
import cv2
import threading
import argparse
import logging
import datetime
import shutil
import os
from enum import Enum

import tensorflow as tf

from tf_pose.estimator import TfPoseEstimator
from tf_pose.networks import get_graph_path

import torch
import torchvision
import torch.nn.parallel
import torch.backends.cudnn as cudnn

from tsn_pytorch.models import TSN
from tsn_pytorch.transforms import *

import tkinter

'''
15 class
action_label -> action_name
1  -> 1         (No Action)
2  -> 2_start   (螺旋丸)
3  -> 2_end
4  -> 3_start   (甩)
5  -> 3_end
6  -> 4_start   (龜派氣功)
7  -> 4_end
8  -> 5_start   (落雷)
9  -> 5_end
10 -> 6         (防禦壹之型)
11 -> 7         (防禦貳之型) 太極
12 -> 8         (防禦參之型) 黑豹
13 -> 9         (防禦肆之型) 體操?
14 -> 10        (防禦伍之型) 結印
15 -> 11        (踢)


13 class
action_label -> action_name
1  -> 1         (No Action)
2  -> 2_start   (螺旋丸)
3  -> 2_end
4  -> 3_start   (甩)
5  -> 3_end
6  -> 4_start   (龜派氣功)
7  -> 4_end
8  -> 5_start   (落雷)
9  -> 5_end
10 -> 6         (防禦壹之型)
11 -> 7         (防禦貳之型) 太極
12 -> 9         (防禦肆之型) 體操?
13 -> 10        (防禦伍之型) 結印


11 class
action_label -> action_name
1  -> 1         (No Action)
2  -> 2_start   (ATK_1) 螺旋丸
3  -> 2_end
4  -> 3         (ATK_2) 甩
5  -> 4_start   (ATK_3) 歸派氣功
6  -> 4_end
7  -> 5         (ATK_4) 落雷
8 -> 6          (DEF_1) 一般?
9 -> 7          (DEF_2) 太極
10 -> 9         (DEF_3) 體操?
11 -> 10        (DEF_4) 結印
'''

class Skill(Enum):          # 9 actions 11 classes
    No_action = 1
    ATK_1_start = 2
    ATK_1_end = 3
    ATK_2 = 4
    ATK_3_start = 5
    ATK_3_end = 6
    ATK_4 = 7
    DEF_1 = 8
    DEF_2 = 9
    DEF_3 = 10
    DEF_4 = 11

############################## Action Recognition ######################################
global num_class
num_class = 11

model = TSN(num_class, 3, 'RGB', base_model='resnet34', consensus_type='avg', dropout=0.7)

checkpoint = torch.load('D:\\Code\\Hololens_Project\\Core\\tsn_pytorch\\pth\\holo_2019_1220_9_actions_11_class_MOD_4_NEW.pth')
print("model epoch {} best prec@1: {}".format(checkpoint['epoch'], checkpoint['best_prec1']))

base_dict = {'.'.join(k.split('.')[1:]): v for k,v in list(checkpoint['state_dict'].items())}
model.load_state_dict(base_dict)

crop_size = model.crop_size

model = torch.nn.DataParallel(model).cuda()
cudnn.benchmark = True

# switch to evaluate mode
model.eval()

trans = trans = torchvision.transforms.Compose([
    GroupScale(int(crop_size)),
    Stack(roll=False),
    ToTorchFormatTensor(div=True)
    ]
)

# warm up
_a = torch.ones([1, 9, 224, 224], dtype=torch.float32)
_b = torch.autograd.Variable(_a, volatile=True)
_c = model(_b)

################################### OpenPose ###########################################
gpu_options = tf.GPUOptions(allow_growth=True)
#e = TfPoseEstimator(get_graph_path('mobilenet_thin'), target_size=(432, 368), tf_config=tf.ConfigProto(gpu_options=gpu_options))
e = TfPoseEstimator(get_graph_path('mobilenet_v2_large'), target_size=(432, 368), tf_config=tf.ConfigProto(gpu_options=gpu_options))
#e = TfPoseEstimator(get_graph_path('cmu'), target_size=(432, 368), tf_config=tf.ConfigProto(gpu_options=gpu_options))

################################### Socket #######################################
HOST = '192.168.60.2'
PORT = 9000

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)    # tcp
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # reuse tcp
sock.bind((HOST, PORT))
sock.listen(5)
print('\nWait for connection...' + ' IP : ' + HOST)


global_action_p0 = 0  # outside camera action
global_action_p1 = 0
gamepoint_p0 = 10.0
gamepoint_p1 = 10.0
p0_win_lose = 0       # 1->win, 2->lose
p1_win_lose = 0       # 1->win, 2->lose
holo_action_p0 = 0
holo_action_p1 = 0
# def_p0 = 0            # p1 攻擊 p0 時，p0 有無防禦成功
# def_p1 = 0
# blood_effect_p0 = 0
# blood_effect_p1 = 0

my_def_fail_p0 = 0
my_def_succ_p0 = 0
def_fail_p0 = 0
def_succ_p0 = 0

my_def_fail_p1 = 0
my_def_succ_p1 = 0
def_fail_p1 = 0
def_succ_p1 = 0

player_id_err_p0 = 0
player_id_err_p1 = 0

wait_time_p0 = 0.0
wait_time_p1 = 0.0

status_data_p0 = b'0,0,0,0,0,0'    # status = "被對方的技能(ATK1)打到, 被對方的技能(ATK2)打到, 被對方的技能(ATK3)打到, 被對方的技能(ATK4)打到, end game, start new game"
status_data_p1 = b'0,0,0,0,0,0'
data_cp = b''

frame_size_cp = None
co_str_cp = ''
#player_count = 0
player_list = ''

def validate(model, rst):

    input_var = torch.autograd.Variable(rst, volatile=True)
    output = model(input_var)

    topk = (1,5)
    maxk = max(topk)
    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    
    return output, pred


def GameSystem(skill_damage=2.0, skill_wait_time=0.5, player_='P0'):
    global gamepoint_p0, gamepoint_p1
    global p0_win_lose, p1_win_lose
    global holo_action_p0, holo_action_p1
    global global_action_p0, global_action_p1
    global my_def_fail_p0, my_def_succ_p0, def_fail_p0, def_succ_p0
    global my_def_fail_p1, my_def_succ_p1, def_fail_p1, def_succ_p1
    global wait_time_p0, wait_time_p1

    
    ############################# p0 看 p1 #############################
    if (player_ == 'P0'):
        if (time.time() - wait_time_p0) >= skill_wait_time:
            ############################# p0 被打到 #############################
            if status_data_p0[0] == b'1'[0]:                 # 如果 p0 被技能(ATK_1)打到
                if holo_action_p0 != Skill.DEF_1.value:      # 如果 p0 沒 做防禦動作
                    my_def_fail_p0 = 1                       # p0 受傷噴血的動畫，這個會透過socket傳給hololens
                    gamepoint_p0 -= skill_damage             # 扣血
                    #print('P0, 我方扣血')
                else:                                        # 如果 p0 有 做防禦動作
                    my_def_succ_p0 = 1                       # 成功防禦，這個會透過socket傳給hololens，顯示防禦特效
                    #print('P0, 我方防禦成功')
                wait_time_p0 = time.time()
            elif status_data_p0[2] == b'1'[0]:               # 如果 p0 被技能(ATK_2)打到
                if holo_action_p0 != Skill.DEF_2.value:
                    my_def_fail_p0 = 1
                    gamepoint_p0 -= skill_damage
                else:
                    my_def_succ_p0 = 1
                wait_time_p0 = time.time()
            elif status_data_p0[4] == b'1'[0]:               # 如果 p0 被技能(ATK_3)打到
                if holo_action_p0 != Skill.DEF_3.value:
                    my_def_fail_p0 = 1
                    gamepoint_p0 -= skill_damage
                else:
                    my_def_succ_p0 = 1
                wait_time_p0 = time.time()
            elif status_data_p0[6] == b'1'[0]:               # 如果 p0 被技能(ATK_4)打到
                if holo_action_p0 != Skill.DEF_4.value:
                    my_def_fail_p0 = 1
                    gamepoint_p0 -= skill_damage
                else:
                    my_def_succ_p0 = 1
                wait_time_p0 = time.time()
        
            ############################# p1 被打到 #############################
            if status_data_p1[0] == b'1'[0]:                 # 如果 p1 被技能(ATK_1)打到
                if holo_action_p1 != Skill.DEF_1.value:      # 如果 p1 沒 做防禦動作
                    def_fail_p0 = 1
                    #print('P0, 對方防禦失敗')
                else:                                        # 如果 p1 有 做防禦動作
                    def_succ_p0 = 1
                    #print('P0, 對方防禦成功')
                wait_time_p0 = time.time()
            elif status_data_p1[2] == b'1'[0]:               # 如果 p1 被技能(ATK_2)打到
                if holo_action_p1 != Skill.DEF_2.value:
                    def_fail_p0 = 1
                else:
                    def_succ_p0 = 1
                wait_time_p0 = time.time()
            elif status_data_p1[4] == b'1'[0]:               # 如果 p1 被技能(ATK_3)打到
                if holo_action_p1 != Skill.DEF_3.value:
                    def_fail_p0 = 1
                else:
                    def_succ_p0 = 1
                wait_time_p0 = time.time()
            elif status_data_p1[6] == b'1'[0]:               # 如果 p1 被技能(ATK_4)打到
                if holo_action_p1 != Skill.DEF_4.value:
                    def_fail_p0 = 1
                else:
                    def_succ_p0 = 1
                wait_time_p0 = time.time()
    ############################# p1 看 p0 #############################
    elif (player_ == 'P1'):
        if (time.time() - wait_time_p1) >= skill_wait_time:
            ############################# p0 被打到 #############################
            if status_data_p0[0] == b'1'[0]:                 # 如果 p0 被技能(ATK_1)打到
                if holo_action_p0 != Skill.DEF_1.value:      # 如果 p0 沒 做防禦動作
                    def_fail_p1 = 1                          # p0 受傷噴血的動畫，這個會透過socket傳給hololens
                    #print('P1, 對方防禦失敗')
                else:                                        # 如果 p0 有 做防禦動作
                    def_succ_p1 = 1                          # 成功防禦，這個會透過socket傳給hololens，顯示防禦特效
                    #print('P1, 對方防禦成功')
                wait_time_p1 = time.time()
            elif status_data_p0[2] == b'1'[0]:               # 如果 p0 被技能(ATK_2)打到
                if holo_action_p0 != Skill.DEF_2.value:
                    def_fail_p1 = 1
                else:
                    def_succ_p1 = 1
                wait_time_p1 = time.time()
            elif status_data_p0[4] == b'1'[0]:               # 如果 p0 被技能(ATK_3)打到
                if holo_action_p0 != Skill.DEF_3.value:
                    def_fail_p1 = 1
                else:
                    def_succ_p1 = 1
                wait_time_p1 = time.time()
            elif status_data_p0[6] == b'1'[0]:               # 如果 p0 被技能(ATK_4)打到
                if holo_action_p0 != Skill.DEF_4.value:
                    def_fail_p1 = 1
                else:
                    def_succ_p1 = 1
                wait_time_p1 = time.time()
        
            ############################# p1 被打到 #############################
            if status_data_p1[0] == b'1'[0]:                 # 如果 p1 被技能(ATK_1)打到
                if holo_action_p1 != Skill.DEF_1.value:      # 如果 p1 沒 做防禦動作
                    my_def_fail_p1 = 1
                    gamepoint_p1 -= skill_damage
                    #print('P1, 我方扣血')
                else:                                        # 如果 p1 有 做防禦動作
                    my_def_succ_p1 = 1
                    #print('P1, 我方防禦成功')
                wait_time_p1 = time.time()
            elif status_data_p1[2] == b'1'[0]:               # 如果 p1 被技能(ATK_2)打到
                if holo_action_p1 != Skill.DEF_2.value:
                    my_def_fail_p1 = 1
                    gamepoint_p1 -= skill_damage
                else:
                    my_def_succ_p1 = 1
                wait_time_p1 = time.time()
            elif status_data_p1[4] == b'1'[0]:               # 如果 p1 被技能(ATK_3)打到
                if holo_action_p1 != Skill.DEF_3.value:
                    my_def_fail_p1 = 1
                    gamepoint_p1 -= skill_damage
                else:
                    my_def_succ_p1 = 1
                wait_time_p1 = time.time()
            elif status_data_p1[6] == b'1'[0]:               # 如果 p1 被技能(ATK_4)打到
                if holo_action_p1 != Skill.DEF_4.value:
                    my_def_fail_p1 = 1
                    gamepoint_p1 -= skill_damage
                else:
                    my_def_succ_p1 = 1
                wait_time_p1 = time.time()

    ############################# 遊戲結束 #############################
    if player_ == 'P0':
        if gamepoint_p0 <= 0.0:
            print('p0 lose')
            p0_win_lose = 2
        elif gamepoint_p1 <= 0.0:
            print('p0 win')
            p0_win_lose = 1
    elif player_ == 'P1':
        if gamepoint_p0 <= 0.0:
            print('p1 win')
            p1_win_lose = 1
        elif gamepoint_p1 <= 0.0:
            print('p1 lose')
            p1_win_lose = 2


class TServer(threading.Thread):
    def __init__(self, socket, adr, count=1, action=0, fps_time=0):
        threading.Thread.__init__(self)
        self.socket = socket
        self.address= adr
        self.count = count
        self.action = action
        self.fps_time = fps_time
        #self.lock = lock

        ##################### Kalman filter ##################
        '''
        它有3个输入参数
        dynam_params  ：状态空间的维数，这里为4
        measure_param ：测量值的维数，这里也为2
        control_params：控制向量的维数，默认为0。由于这里该模型中并没有控制变量，因此也为0。
        kalman.processNoiseCov    ：为模型系统的噪声，噪声越大，预测结果越不稳定，越容易接近模型系统预测值，且单步变化越大，相反，若噪声小，则预测结果与上个计算结果相差不大。
        kalman.measurementNoiseCov：为测量系统的协方差矩阵，方差越小，预测结果越接近测量值
        '''
        processNoiseCov = 1e-4
        measurementNoiseCov = 0.01

        self.kalman_RShoulder = cv2.KalmanFilter(4,2)
        self.kalman_RShoulder.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_RShoulder.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_RShoulder.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_RShoulder.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_RShoulder.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_RElbow = cv2.KalmanFilter(4,2)
        self.kalman_RElbow.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_RElbow.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_RElbow.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_RElbow.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_RElbow.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_RWrist = cv2.KalmanFilter(4,2)
        self.kalman_RWrist.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_RWrist.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_RWrist.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_RWrist.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_RWrist.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_LShoulder = cv2.KalmanFilter(4,2)
        self.kalman_LShoulder.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_LShoulder.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_LShoulder.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_LShoulder.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_LShoulder.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_LElbow = cv2.KalmanFilter(4,2)
        self.kalman_LElbow.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_LElbow.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_LElbow.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_LElbow.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_LElbow.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_LWrist = cv2.KalmanFilter(4,2)
        self.kalman_LWrist.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_LWrist.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_LWrist.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_LWrist.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_LWrist.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_RHip = cv2.KalmanFilter(4,2)
        self.kalman_RHip.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_RHip.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_RHip.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_RHip.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_RHip.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_RKnee = cv2.KalmanFilter(4,2)
        self.kalman_RKnee.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_RKnee.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_RKnee.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_RKnee.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_RKnee.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_RAnkle = cv2.KalmanFilter(4,2)
        self.kalman_RAnkle.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_RAnkle.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_RAnkle.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_RAnkle.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_RAnkle.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_LHip = cv2.KalmanFilter(4,2)
        self.kalman_LHip.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_LHip.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_LHip.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_LHip.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_LHip.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_LKnee = cv2.KalmanFilter(4,2)
        self.kalman_LKnee.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_LKnee.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_LKnee.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_LKnee.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_LKnee.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.kalman_LAnkle = cv2.KalmanFilter(4,2)
        self.kalman_LAnkle.measurementMatrix = np.array([[1,0,0,0],[0,1,0,0]],np.float32)
        self.kalman_LAnkle.transitionMatrix = np.array([[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]], np.float32)
        self.kalman_LAnkle.processNoiseCov = np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], np.float32) * processNoiseCov
        self.kalman_LAnkle.measurementNoiseCov = np.array([[1,0],[0,1]], np.float32) * measurementNoiseCov
        self.kalman_LAnkle.errorCovPost = np.array([[1,0],[0,1]], np.float32) * 1

        self.ori_RShoulder = np.array([[0],[0]],np.float32)
        self.ori_RElbow = np.array([[0],[0]],np.float32)
        self.ori_RWrist = np.array([[0],[0]],np.float32)
        self.ori_LShoulder = np.array([[0],[0]],np.float32)
        self.ori_LElbow = np.array([[0],[0]],np.float32)
        self.ori_LWrist = np.array([[0],[0]],np.float32)
        self.ori_RHip = np.array([[0],[0]],np.float32)
        self.ori_RKnee = np.array([[0],[0]],np.float32)
        self.ori_RAnkle = np.array([[0],[0]],np.float32)
        self.ori_LHip = np.array([[0],[0]],np.float32)
        self.ori_LKnee = np.array([[0],[0]],np.float32)
        self.ori_LAnkle = np.array([[0],[0]],np.float32)

        self.pre_RShoulder = np.array([[0],[0]],np.float32)
        self.pre_RElbow = np.array([[0],[0]],np.float32)
        self.pre_RWrist = np.array([[0],[0]],np.float32)
        self.pre_LShoulder = np.array([[0],[0]],np.float32)
        self.pre_LElbow = np.array([[0],[0]],np.float32)
        self.pre_LWrist = np.array([[0],[0]],np.float32)
        self.pre_RHip = np.array([[0],[0]],np.float32)
        self.pre_RKnee = np.array([[0],[0]],np.float32)
        self.pre_RAnkle = np.array([[0],[0]],np.float32)
        self.pre_LHip = np.array([[0],[0]],np.float32)
        self.pre_LKnee = np.array([[0],[0]],np.float32)
        self.pre_LAnkle = np.array([[0],[0]],np.float32)
        

    def run(self):
        #global player_count
        global player_list, player_id_err_p0, player_id_err_p1

        print(datetime.datetime.now().strftime('%m/%d %H:%M:%S '), end='')
        print ('Client %s:%s connected.' % self.address)
        window_name = str(self.address[1])
        #print('port : %s' % window_name)

        ID = self.socket.recv(1024)
        print(ID)
        self.socket.send(b'OK')

        if ID == b'holo_P0':
            #player_count += 1
            if player_list.find('0') == -1:       # 若沒有人選擇 player 0
                player_list += '0'
                self.run_holo(window_name, 'P0')
            else:                                 # 若已經有人選擇 player 0
                print('Wrong player ID selected, the system has automatically selected another player ID.')
                player_id_err_p0 = 1
                player_list += '1'
                self.run_holo(window_name, 'P1')  # 把 player ID 改成 P1
        elif ID == b'holo_P1':
            #player_count += 1
            if player_list.find('1') == -1:
                player_list += '1'
                self.run_holo(window_name, 'P1')
            else:
                print('Wrong player ID selected, the system has automatically selected another player ID.')
                player_id_err_p1 = 1
                player_list += '0'
                self.run_holo(window_name, 'P0')  # 把 player ID 改成 P0
        elif ID == b'4cam':
            self.run_4cam(window_name)
        elif ID == b'Unity_demo':
            self.run_demo(window_name)
        elif ID == b'bye':
            print(datetime.datetime.now().strftime('%m/%d %H:%M:%S '), end='')
            print ('Client %s:%s disconnected.' % self.address)
            self.socket.close()
        else:
            print('wrong ID' + ' : ' + str(ID))
            self.socket.close()

    def openpose_coordinate_to_str(self, key_points):
        my_str = ''
        for i in range(len(key_points)):  # 18個點，36個值(x,y)
            my_str = my_str + str(key_points[i][0]) + ',' + str(key_points[i][1]) + ','
        return my_str

    def run_holo_reset(self):
        global p0_win_lose, p1_win_lose
        global gamepoint_p0, gamepoint_p1
        global holo_action_p0, holo_action_p1

        holo_action_p0 = 1
        holo_action_p1 = 1
        p0_win_lose = 0
        p1_win_lose = 0
        gamepoint_p0 = 10.0
        gamepoint_p1 = 10.0

    def run_demo(self, window_name):
        global frame_size_cp
        global data_cp
        global co_str_cp


        while(True):
            zz = self.socket.recv(1024)
            if zz == b'bye':
                break
            # print(zz)
            # frame_size_int = int.from_bytes(frame_size, byteorder='big')
            # print(frame_size_int)
            self.socket.send(frame_size_cp)
            self.socket.send(data_cp)
            self.socket.send(co_str_cp)

        print(datetime.datetime.now().strftime('%m/%d %H:%M:%S '), end='')
        print ('Client %s:%s disconnected. (run_demo)' % self.address)
        self.socket.close()


    def run_holo(self, window_name, player):

        global global_action_p0, global_action_p1
        global gamepoint_p0, gamepoint_p1
        global p0_win_lose, p1_win_lose
        global holo_action_p0, holo_action_p1
        global status_data_p0, status_data_p1
        global my_def_fail_p0, my_def_succ_p0, def_fail_p0, def_succ_p0
        global my_def_fail_p1, my_def_succ_p1, def_fail_p1, def_succ_p1
        global player_list, player_id_err_p0, player_id_err_p1

        global frame_size_cp
        global data_cp
        global co_str_cp

        print(player_list)

        images = list()
        final_action = 0

        action_window = list()          # action sliding window
        for i in range(3):                 # sliding window size = 3
            action_window.append(Skill.No_action.value)

        fps_array = list()                 # FSP sliding window
        for i in range(5):                 # sliding window size = 5
            fps_array.append(15.0)
        
        avg_fps = 15.0
        step_size = 3

        ################### for debug ###################
        # temp_cou = 0
        # debug_root_path = 'debug/9_actions_11_class_MOD_4_0/'
        # if not os.path.exists(debug_root_path):
        #     os.mkdir(debug_root_path)
        # else:
        #     shutil.rmtree(debug_root_path)
        #     os.mkdir(debug_root_path)
        # debug_file = open(debug_root_path + 'result.txt', 'w')


        while(True):
            no_human = 0   # no human flag
            data = b''
            try:
                temp_data = self.socket.recv(4096)
                frame_size = temp_data[:4]
                frame_size_int = int.from_bytes(frame_size, byteorder='big')
                #print(player + ' ' + str(frame_size_int))
                temp_data = temp_data[4:]

                data += temp_data
                while(True):
                    if len(data) == frame_size_int + 256:
                        break
                    temp_data = self.socket.recv(4096)
                    data += temp_data
            except ConnectionResetError:    # 當 hololens 關閉時
                self.run_holo_reset()
                break
            except ConnectionAbortedError:  # 當 hololens 關閉時
                self.run_holo_reset()
                break

            img_data = data[0:frame_size_int]              # 封包的前段是 HoloLens 傳過來的 image data
            frame = np.fromstring(img_data, dtype=np.uint8)
            dec_img = cv2.imdecode(frame, 1)               # 解碼成可以讀取的影像檔
            crop_img = dec_img.copy()                      # 複製一份給後面的crop用

            if player == 'P0':
                status_data_p0 = data[frame_size_int:]     # 封包的後段才是 HoloLens 傳過來的 status
            elif player == 'P1':                           # status = "被對方的技能(ATK1)打到, 被對方的技能(ATK2)打到, 被對方的技能(ATK3)打到, 被對方的技能(ATK4)打到, end game, start new game"
                status_data_p1 = data[frame_size_int:]


            humans = e.inference(dec_img, resize_to_default=True, upsample_size=4.0)
            img, key_points = TfPoseEstimator.draw_one_human(dec_img, humans, imgcopy=False, score=0.8)

            del fps_array[0]
            fps_array.append(1.0 / (time.time() - self.fps_time))
            avg_fps = sum(fps_array)/5.0
            if avg_fps > 16.0:
                step_size = 3
            elif avg_fps > 10.0:
                step_size = 2
            else:
                step_size = 1

            cv2.putText(img, "FPS: %f" % (avg_fps), (10, 10),  cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            cv2.imshow(window_name + player, img)
            self.fps_time = time.time()

            ####################### Kalman filter #######################
            if(key_points[2][0] != 0 or key_points[2][1] != 0):
                self.ori_RShoulder = np.array([[key_points[2][0]],[key_points[2][1]]], np.float32)
                self.kalman_RShoulder.correct(self.ori_RShoulder)
                self.pre_RShoulder = self.kalman_RShoulder.predict()
                key_points[2][0] = self.pre_RShoulder[0,0]
                key_points[2][1] = self.pre_RShoulder[1,0]

            if(key_points[3][0] != 0 or key_points[3][1] != 0):
                self.ori_RElbow = np.array([[key_points[3][0]],[key_points[3][1]]], np.float32)
                self.kalman_RElbow.correct(self.ori_RElbow)
                self.pre_RElbow = self.kalman_RElbow.predict()
                key_points[3][0] = self.pre_RElbow[0,0]
                key_points[3][1] = self.pre_RElbow[1,0]
            
            if(key_points[4][0] != 0 or key_points[4][1] != 0):
                self.ori_RWrist = np.array([[key_points[4][0]],[key_points[4][1]]], np.float32)
                self.kalman_RWrist.correct(self.ori_RWrist)
                self.pre_RWrist = self.kalman_RWrist.predict()
                key_points[4][0] = self.pre_RWrist[0,0]
                key_points[4][1] = self.pre_RWrist[1,0]

            if(key_points[5][0] != 0 or key_points[5][1] != 0):
                self.ori_LShoulder = np.array([[key_points[5][0]],[key_points[5][1]]], np.float32)
                self.kalman_LShoulder.correct(self.ori_LShoulder)
                self.pre_LShoulder = self.kalman_LShoulder.predict()
                key_points[5][0] = self.pre_LShoulder[0,0]
                key_points[5][1] = self.pre_LShoulder[1,0]

            if(key_points[6][0] != 0 or key_points[6][1] != 0):
                self.ori_LElbow = np.array([[key_points[6][0]],[key_points[6][1]]], np.float32)
                self.kalman_LElbow.correct(self.ori_LElbow)
                self.pre_LElbow = self.kalman_LElbow.predict()
                key_points[6][0] = self.pre_LElbow[0,0]
                key_points[6][1] = self.pre_LElbow[1,0]
            
            if(key_points[7][0] != 0 or key_points[7][1] != 0):
                self.ori_LWrist = np.array([[key_points[7][0]],[key_points[7][1]]], np.float32)
                self.kalman_LWrist.correct(self.ori_LWrist)
                self.pre_LWrist = self.kalman_LWrist.predict()
                key_points[7][0] = self.pre_LWrist[0,0]
                key_points[7][1] = self.pre_LWrist[1,0]
            
            if(key_points[8][0] != 0 or key_points[8][1] != 0):
                self.ori_RHip = np.array([[key_points[8][0]],[key_points[8][1]]], np.float32)
                self.kalman_RHip.correct(self.ori_RHip)
                self.pre_RHip = self.kalman_RHip.predict()
                key_points[8][0] = self.pre_RHip[0,0]
                key_points[8][1] = self.pre_RHip[1,0]

            if(key_points[9][0] != 0 or key_points[9][1] != 0):
                self.ori_RKnee = np.array([[key_points[9][0]],[key_points[9][1]]], np.float32)
                self.kalman_RKnee.correct(self.ori_RKnee)
                self.pre_RKnee = self.kalman_RKnee.predict()
                key_points[9][0] = self.pre_RKnee[0,0]
                key_points[9][1] = self.pre_RKnee[1,0]
            
            if(key_points[10][0] != 0 or key_points[10][1] != 0):
                self.ori_RAnkle = np.array([[key_points[10][0]],[key_points[10][1]]], np.float32)
                self.kalman_RAnkle.correct(self.ori_RAnkle)
                self.pre_RAnkle = self.kalman_RAnkle.predict()
                key_points[10][0] = self.pre_RAnkle[0,0]
                key_points[10][1] = self.pre_RAnkle[1,0]

            if(key_points[11][0] != 0 or key_points[11][1] != 0):
                self.ori_LHip = np.array([[key_points[11][0]],[key_points[11][1]]], np.float32)
                self.kalman_LHip.correct(self.ori_LHip)
                self.pre_LHip = self.kalman_LHip.predict()
                key_points[11][0] = self.pre_LHip[0,0]
                key_points[11][1] = self.pre_LHip[1,0]

            if(key_points[12][0] != 0 or key_points[12][1] != 0):
                self.ori_LKnee = np.array([[key_points[12][0]],[key_points[12][1]]], np.float32)
                self.kalman_LKnee.correct(self.ori_LKnee)
                self.pre_LKnee = self.kalman_LKnee.predict()
                key_points[12][0] = self.pre_LKnee[0,0]
                key_points[12][1] = self.pre_LKnee[1,0]
            
            if(key_points[13][0] != 0 or key_points[13][1] != 0):
                self.ori_LAnkle = np.array([[key_points[13][0]],[key_points[13][1]]], np.float32)
                self.kalman_LAnkle.correct(self.ori_LAnkle)
                self.pre_LAnkle = self.kalman_LAnkle.predict()
                key_points[13][0] = self.pre_LAnkle[0,0]
                key_points[13][1] = self.pre_LAnkle[1,0]

            co_str = self.openpose_coordinate_to_str(key_points)

            if key_points[1][0] == 0 and key_points[1][1] == 0 and key_points[8][0] == 0 and key_points[8][1] == 0 and key_points[11][0] == 0 and key_points[11][1] == 0:    # Neck, RHip, LHip == 0  =>  No human
                x1 = 224 - 126
                x2 = 224 + 126
                no_human = 1
            elif key_points[1][0]-126 < 0:
                x1 = 0
                x2 = 252
            elif key_points[1][0]+126 > 447:
                x1 = 196
                x2 = 448
            else:
                x1 = key_points[1][0] - 126
                x2 = key_points[1][0] + 126

            crop_img = crop_img[:, x1:x2, :].copy()
            #cv2.imshow('TSN', crop_img)

            img_tsn = Image.fromarray(cv2.cvtColor(crop_img, cv2.COLOR_BGR2RGB))

            if self.count % step_size == 0 and len(images) == 2:
                images.extend([img_tsn])
                # print(images[0].size)
                rst = trans(images)
                rst = torch.unsqueeze(rst, 0)
                my_output, my_pred = validate(model, rst)         # predict action

                self.action = int(my_pred[0][0])
                top2 = int(my_pred[1][0])
                top3 = int(my_pred[2][0])
                top4 = int(my_pred[3][0])
                top5 = int(my_pred[4][0])

                ################### for debug ###################
                # if no_human == 0:
                #     detail = '{:d} ({:.2f}), {:d} ({:.2f}), {:d} ({:.2f}), {:d} ({:.2f}), {:d} ({:.2f})'.format(
                #         self.action, float(my_output[0][int(my_pred[0][0])]),
                #         top2, float(my_output[0][top2]),
                #         top3, float(my_output[0][top3]),
                #         top4, float(my_output[0][top4]),
                #         top5, float(my_output[0][top5])
                #     )
                #     img_dir = debug_root_path + '{:04d}.jpg'.format(temp_cou)
                #     cv2.imwrite(img_dir, crop_img, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
                #     debug_file.write(detail + '\n')
                #     temp_cou += 1

                if (self.action == 0):
                    self.action = num_class

                del images[0]

                # if no_human == 1:                                          # 若其中一位玩家沒看著對方
                #     # holo_action_p0 = global_action_p0
                #     # holo_action_p1 = global_action_p1
                #     holo_action_p0 = Skill.No_action.value
                #     holo_action_p1 = Skill.No_action.value
                #     del action_window[0]
                #     action_window.append(Skill.No_action.value)
                # elif (player == 'P0' and status_data_p0[10] == b'1'[0]):   # HoloLens 端 P0 已經進入end game畫面
                #     holo_action_p1 = Skill.No_action.value                 # 遊戲 reset 階段辨識到的動作一率為 No action
                #     del action_window[0]
                #     action_window.append(Skill.No_action.value)
                # elif (player == 'P1' and status_data_p1[10] == b'1'[0]):   # HoloLens 端 P1 已經進入end game畫面
                #     holo_action_p0 = Skill.No_action.value                 # 遊戲 reset 階段辨識到的動作一率為 No action
                #     del action_window[0]
                #     action_window.append(Skill.No_action.value)
                # else:
                del action_window[0]

                if (final_action == Skill.ATK_1_start.value):                                             # 如果上一個動作 是 螺旋丸的 start
                    if (self.action == Skill.ATK_1_start.value or self.action == Skill.ATK_1_end.value):  # 如果現在的動作 是 螺旋丸的 start 或是 螺旋丸的 end
                        action_window.append(self.action)
                    elif (top2 == Skill.ATK_1_start.value or top2 == Skill.ATK_1_end.value):
                        action_window.append(top2)
                    elif (top3 == Skill.ATK_1_start.value or top3 == Skill.ATK_1_end.value):
                        action_window.append(top3)
                    else:
                        action_window.append(self.action)
                elif (final_action == Skill.ATK_3_start.value):                                           # 如果上一個動作 是 歸派氣功的 start
                    if (self.action == Skill.ATK_3_start.value or self.action == Skill.ATK_3_end.value):  # 如果現在的動作 是 歸派氣功的 start 或是 螺旋丸的 end
                        action_window.append(self.action)
                    elif (top2 == Skill.ATK_3_start.value or top2 == Skill.ATK_3_end.value):
                        action_window.append(top2)
                    elif (top3 == Skill.ATK_3_start.value or top3 == Skill.ATK_3_end.value):
                        action_window.append(top3)
                    else:
                        action_window.append(self.action)
                else:
                    action_window.append(self.action)

                temp = np.array([], np.int32)
                for i in range(num_class + 1):                              # 因為 action 從 1 開始，而非 0，這樣後面取 argmax 才會正確
                    temp = np.append(temp, action_window.count(i))
                
                final_action = np.argmax(temp)

                if player == 'P0':
                    holo_action_p1 = final_action
                elif player == 'P1':
                    holo_action_p0 = final_action

                #print(action_window)

            elif self.count % step_size == 0:
                images.extend([img_tsn])

            '''
            if no_human == 1:                                          # 若其中一位玩家沒看著對方
                # holo_action_p0 = global_action_p0
                # holo_action_p1 = global_action_p1
                holo_action_p0 = Skill.No_action.value
                holo_action_p1 = Skill.No_action.value
            '''

            # with open('Debug_for_multiplayer.txt', 'r') as f:
            #     debug_action = f.readline()
            # holo_action_p0 = int(debug_action)

            if (player == 'P0'):
                if status_data_p0[10] == b'1'[0] or gamepoint_p0 == 0.0:   # HoloLens 端 P0 已經進入end game畫面
                    holo_action_p0 = Skill.No_action.value                 # 遊戲 reset 階段辨識到的動作一率為 No action
                    holo_action_p1 = Skill.No_action.value
            if (player == 'P1'):
                if status_data_p1[10] == b'1'[0] or gamepoint_p1 == 0.0:   # HoloLens 端 P1 已經進入end game畫面
                    holo_action_p0 = Skill.No_action.value                 # 遊戲 reset 階段辨識到的動作一率為 No action
                    holo_action_p1 = Skill.No_action.value


            GameSystem(skill_damage=2.5, skill_wait_time=0.5, player_=player)                     # 經過 GameSystem 判定雙方分數


            co_str = co_str + str(self.count) + ',' + str(holo_action_p0) + ',' + str(holo_action_p1)
            co_str = co_str + ',' + str(gamepoint_p0) + ',' + str(gamepoint_p1) + ',' + str(p0_win_lose) + ',' + str(p1_win_lose)
            co_str = co_str + ',' + str(my_def_fail_p0) + ',' + str(my_def_succ_p0) + ',' + str(def_fail_p0) + ',' + str(def_succ_p0)
            co_str = co_str + ',' + str(my_def_fail_p1) + ',' + str(my_def_succ_p1) + ',' + str(def_fail_p1) + ',' + str(def_succ_p1)
            co_str = co_str + ',' + str(player_id_err_p0) + ',' + str(player_id_err_p1)

            ###### Debug for 3D position estimation ######
            # with open('Debug_for_3D_position_estimation.txt', 'r') as f:
            #     pre_z = f.readline()
            # co_str = co_str + ',' + str(pre_z.strip())

            #print(co_str)
            co_str = bytes(co_str, 'ascii')
            co_str = co_str + b',' + status_data_p0 + b',' + status_data_p1

            try:
                self.socket.send(co_str)
            except ConnectionResetError:    # 當 hololens 關閉時
                self.run_holo_reset()
                break
            except ConnectionAbortedError:  # 當 hololens 關閉時
                self.run_holo_reset()
                break

            # ------------ reset data ------------ #
            if (player == 'P0'):
                my_def_fail_p0 = 0
                my_def_succ_p0 = 0
                def_fail_p0 = 0
                def_succ_p0 = 0

                if (status_data_p0[10] == b'1'[0] and len(player_list) == 1):           # 當player數只有一人(自己)，需要把對方的輸贏標記reset，不然等對手接上Server時會錯亂
                    p0_win_lose = 0
                    p1_win_lose = 0
                    gamepoint_p0 = 10.0
                    #print('one player reset(P0)')
                elif (status_data_p0[10] == b'1'[0] and status_data_p1[10] == b'1'[0] and len(player_list) == 2):
                    p0_win_lose = 0
                    gamepoint_p0 = 10.0
                    print('---------- P0 Game system ready ----------')
            
            if (player == 'P1'):
                my_def_fail_p1 = 0
                my_def_succ_p1 = 0
                def_fail_p1 = 0
                def_succ_p1 = 0

                if (status_data_p1[10] == b'1'[0] and len(player_list) == 1):
                    p0_win_lose = 0
                    p1_win_lose = 0
                    gamepoint_p1 = 10.0
                    #print('one player reset(P1)')
                elif (status_data_p0[10] == b'1'[0] and status_data_p1[10] == b'1'[0] and len(player_list) == 2):
                    p1_win_lose = 0
                    gamepoint_p1 = 10.0
                    print('---------- P1 Game system ready ----------')
            

            # global_action_p0 = Skill.No_action.value
            # global_action_p1 = Skill.No_action.value
            

            ####################### for unity_demo img_data #######################
            # if player == 'P0':
            #     frame_size_cp = frame_size
            #     data_cp = img_data
            #     co_str_cp = co_str
            
            
            self.count += 1
            if cv2.waitKey(1) == 27:
                break

        cv2.destroyWindow(window_name + player)
        print(datetime.datetime.now().strftime('%m/%d %H:%M:%S '), end='')
        print ('Client %s:%s disconnected.' % self.address, end='')
        print('(' + player + ')')
        #player_count -= 1
        if (player == 'P0'):
            player_list = player_list.replace('0', '')
        if (player == 'P1'):
            player_list = player_list.replace('1', '')
        print(player_list)
        self.socket.close()
        #debug_file.close()

    def run_4cam(self, window_name):
        global global_action_p0
        global global_action_p1
        while(True):
            action_byte = self.socket.recv(1024)
            if action_byte == b'bye':
                break
            #print(action_byte)
            act_p0 = int(bytes.decode(action_byte).split(',')[0])
            act_p1 = int(bytes.decode(action_byte).split(',')[1])

            global_action_p0 = int((act_p0 + 2)/4) + 1
            global_action_p1 = int((act_p1 + 2)/4) + 1

            print(str(global_action_p0) + ', ' + str(global_action_p1))

            '''
            if action_byte == b'1':
                global_action = 1
            elif action_byte == b'2' or action_byte == b'3' or action_byte == b'4' or action_byte == b'5':
                global_action = 2
            elif action_byte == b'6' or action_byte == b'7' or action_byte == b'8' or action_byte == b'9':
                global_action = 3
            elif action_byte == b'10' or action_byte == b'11' or action_byte == b'12' or action_byte == b'13':
                global_action = 4
            elif action_byte == b'14' or action_byte == b'15' or action_byte == b'16' or action_byte == b'17':
                global_action = 5
            elif action_byte == b'18' or action_byte == b'19' or action_byte == b'20' or action_byte == b'21':
                global_action = 6
            '''
                
            self.socket.send(b'OK')

        print(datetime.datetime.now().strftime('%m/%d %H:%M:%S '), end='')
        print ('Client %s:%s disconnected. (run_4cam)' % self.address)
        self.socket.close()

'''
class GameSystem(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        global gamepoint_p0, gamepoint_p1
        global p0_win_lose, p1_win_lose
        global holo_action_p0, holo_action_p1
        global global_action_p0, global_action_p1
        global def_p0, def_p1
        global blood_effect_p0, blood_effect_p1

        skill_2_damage = 2
        #action2_start_p0 = 0
        #action2_start_p1 = 0

        skill_wait_time = 1      # 對方施放每一招，都會有一個等待時間(硬直2秒)
        restart_wait_time = 5    # GameSystem 重啟等待時間

        while(True):
            ############################# p0 看 p1 #############################
            if status_data_p0[0] == b'1'[0]:        # 如果 p0 被技能(Skill_2)打到
                if holo_action_p0 != 10:            # 如果 p0 沒 做防禦動作
                    blood_effect_p0 = 1             # p0 受傷噴血的動畫，這個會透過socket傳給hololens
                    gamepoint_p0 -= skill_2_damage  # 扣血
                else:                               # 如果 p0 有 做防禦動作
                    def_p0 = 1          # 成功防禦，這個會透過socket傳給hololens，顯示防禦特效
                
                if gamepoint_p0 != 0:
                    time.sleep(0.5)
                    blood_effect_p0 = 0             # init
                    def_p0 = 0          # init
                    time.sleep(skill_wait_time)

            ############################# p1 看 p0 #############################
            if status_data_p1[0] == b'1'[0]:   # 如果 p1 被技能(Skill_2)打到
                if holo_action_p1 != 10:       # 如果 p1 沒 做防禦動作
                    blood_effect_p1 = 1
                    gamepoint_p1 -= skill_2_damage
                else:                          # 如果 p1 有 做防禦動作
                    def_p1 = 1
                
                if gamepoint_p1 != 0:
                    time.sleep(0.5)
                    blood_effect_p1 = 0       # init
                    def_p1 = 0    # init
                    time.sleep(skill_wait_time)
            
            ############################# 遊戲結束，結算，reset #############################
            if gamepoint_p0 == 0:
                print('p0 lose, p1 win')
                p0_win_lose = 2     # p0 lose
                p1_win_lose = 1     # p1 win
                time.sleep(0.5)
                blood_effect_p0 = 0 # init
                # ------------ reset data ------------ #
                p0_win_lose = 0
                p1_win_lose = 0
                gamepoint_p0 = 10
                gamepoint_p1 = 10
                holo_action_p0 = 1
                holo_action_p1 = 1
                global_action_p0 = 1
                global_action_p1 = 1
                print('---------- Game system ready ----------')
                time.sleep(1)
                
            elif gamepoint_p1 == 0:
                print('p0 win, p1 lose')
                p0_win_lose = 1     # p0 lose
                p1_win_lose = 2     # p1 win
                time.sleep(0.5)
                blood_effect_p1 = 0 # init
                # ------------ reset data ------------ #
                p0_win_lose = 0
                p1_win_lose = 0
                gamepoint_p0 = 10
                gamepoint_p1 = 10
                holo_action_p0 = 1
                holo_action_p1 = 1
                global_action_p0 = 1
                global_action_p1 = 1
                print('---------- Game system ready ----------')
                time.sleep(1)
                
            time.sleep(0.03)
'''

if __name__ == '__main__':
    #GameSystem().start()

    while True:
        (client, adr) = sock.accept()
        TServer(client, adr, count=1, action=0, fps_time=0).start()
