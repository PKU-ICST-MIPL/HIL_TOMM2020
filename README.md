# Description

This is the source code of our ACM TOMM 2019 paper "RCE-HIL: Recognizing Cross-media Entailment with Heterogeneous Interactive Learning". Please cite the following paper if you use the resources.

Xin Huang, Yuxin Peng and Zhang Wen, "RCE-HIL: Recognizing Cross-media Entailment with Heterogeneous Interactive Learning", ACM Transactions on Multimedia Computing, Communications, and Applications (TOMM), 2019.

## Environment
python 3.6

tensorflow 1.3

tqdm

nltk

parse

## Preparation
	$ cd HIL
	$ pip install -r requirements.txt

## Data Preparation

You can download the [SNLI] (https://drive.google.com/file/d/1CxjKsaM6YgZPRKmJhNn7WcIC3gISehcS/view?usp=sharing) dataset and [Flickr30K] images (http://shannon.cs.illinois.edu/DenotationGraph/data/index.html) used in our paper. All the data files should be unzipped and saved in directory data/ (You need to create the directory data under directory TOMM_HIL).

## Training and testing
	$ cd python 
	$ sh run.sh

After running this script, you can train the model and obtain the testing results. 
