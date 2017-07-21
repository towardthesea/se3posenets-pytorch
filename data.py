import csv
import torch
import numpy as np
import cv2
import os
from torch.utils.data import Dataset


############
### Helper functions for reading baxter data

# Read baxter state files
def read_baxter_state_file(filename):
    ret = {}
    with open(filename, 'rb') as csvfile:
        spamreader = csv.reader(csvfile, delimiter=' ', quoting=csv.QUOTE_NONNUMERIC)
        ret['actjtpos']     = torch.Tensor(spamreader.next()[0:-1])  # Last element is a string due to the way the file is created
        ret['actjtvel']     = torch.Tensor(spamreader.next()[0:-1])
        ret['actjteff']     = torch.Tensor(spamreader.next()[0:-1])
        ret['comjtpos']     = torch.Tensor(spamreader.next()[0:-1])
        ret['comjtvel']     = torch.Tensor(spamreader.next()[0:-1])
        ret['comjtacc']     = torch.Tensor(spamreader.next()[0:-1])
        ret['tarendeffpos'] = torch.Tensor(spamreader.next()[0:-1])
    return ret


# Read baxter SE3 state file for all the joints
def read_baxter_se3state_file(filename):
    # Read all the lines in the SE3-state file
    lines = []
    with open(filename, 'rb') as csvfile:
        spamreader = csv.reader(csvfile, delimiter=' ', quoting=csv.QUOTE_NONNUMERIC)
        for row in spamreader:
            if len(row) == 0:
                continue
            if type(row[-1]) == str:  # In case we have a string at the end of the list
                row = row[0:-1]
            lines.append(torch.Tensor(row))
    # Parse the SE3-states
    ret, ctr = {}, 0
    while (ctr < len(lines)):
        id = int(lines[ctr][0])  # ID of mesh
        data = lines[ctr + 1].view(3, 4)  # Transform data
        T = torch.eye(4)
        T[0:3, 0:4] = torch.cat([data[0:3, 1:4], data[0:3, 0]], 1)  # [R | t; 0 | 1]
        ret[id] = T  # Add to list of transforms
        ctr += 2  # Increment counter
    return ret


# Read baxter joint labels and their corresponding mesh index value
def read_baxter_labels_file(filename):
    ret = {}
    with open(filename, 'rb') as csvfile:
        spamreader      = csv.reader(csvfile, delimiter=' ')
        ret['frames']   = spamreader.next()[0:-1]
        ret['meshIds']  = torch.IntTensor([int(x) for x in spamreader.next()[0:-1]])

    return ret


# Read baxter camera data file
def read_cameradata_file(filename):
    # Read lines in the file
    lines = []
    with open(filename, 'rb') as csvfile:
        spamreader = csv.reader(csvfile, delimiter=' ')
        for row in spamreader:
            lines.append([x for x in row if x != ''])
    # Compute modelview and camera parameter matrix
    ret = {}
    ret['modelView'] = torch.Tensor([float(x) for x in lines[1] + lines[2] + lines[3] + lines[4]]).view(4, 4).clone()
    ret['camParam']  = torch.Tensor([float(x) for x in lines[6] + lines[7] + lines[8] + lines[9]]).view(4, 4).clone()
    return ret


############
### Helper functions for reading image data

# Read depth image from disk
def read_depth_image(filename, ht=240, wd=320, scale=1e-4):
    imgf = cv2.imread(filename, -1).astype(
        np.int16) * scale  # Read image (unsigned short), convert to short & scale to get float
    if (imgf.shape[0] != int(ht) or imgf.shape[1] != int(wd)):
        imgscale = cv2.resize(imgf, (int(wd), int(ht)),
                              interpolation=cv2.INTER_NEAREST)  # Resize image with no interpolation (NN lookup)
    else:
        imgscale = imgf
    return torch.Tensor(imgscale).unsqueeze(0)  # Add extra dimension


# Read flow image from disk
def read_flow_image_xyz(filename, ht=240, wd=320, scale=1e-4):
    imgf = cv2.imread(filename, -1).astype(
        np.int16) * scale  # Read image (unsigned short), convert to short & scale to get float
    if (imgf.shape[0] != int(ht) or imgf.shape[1] != int(wd)):
        imgscale = cv2.resize(imgf, (int(wd), int(ht)),
                              interpolation=cv2.INTER_NEAREST)  # Resize image with no interpolation (NN lookup)
    else:
        imgscale = imgf
    return torch.Tensor(imgscale.transpose((2, 0, 1)))  # NOTE: OpenCV reads BGR so it's already xyz when it is read


############
###  SETUP DATASETS: RECURRENT VERSIONS FOR BAXTER DATA - FROM NATHAN'S BAG FILE

### Helper functions for reading the data directories & loading train/test files
def read_recurrent_baxter_dataset(load_dirs, img_suffix, step_len, seq_len, train_per=0.6, val_per=0.15):
    # Get all the load directories
    load_dir_splits = load_dirs.split(',,')  # Get all the load directories
    assert (train_per + val_per < 1);  # Train + val < test

    # Iterate over each load directory to find the datasets
    datasets = []
    for load_dir in load_dir_splits:
        # Get folder names & data statistics for a single load-directory
        dirs = os.listdir(load_dir)
        for dir in dirs:
            path = os.path.join(load_dir, dir) + '/'
            if (os.path.isdir(path)):
                # Get number of images in the folder
                statsfilename = os.path.join(path, 'postprocessstats.txt')
                assert (os.path.exists(statsfilename))
                with open(statsfilename, 'rb') as csvfile:
                    reader = csv.reader(csvfile, delimiter=' ', quoting=csv.QUOTE_NONNUMERIC)
                    nimages = int(reader.next()[0])
                    print('Found {} images in the dataset: {}'.format(int(nimages), path))

                # Setup training and test splits in the dataset
                ntrain = int(train_per * nimages)  # Use first train_per images for training
                nval = int(val_per * nimages)  # Use next val_per images for validation set
                ntest = int(nimages - (ntrain + nval))  # Use remaining images as test set

                dataset = {'path'   : path,
                           'suffix' : img_suffix,
                           'step'   : step_len,
                           'seq'    : seq_len,
                           'numdata': nimages,
                           'train'  : [0, ntrain - 1],
                           'val'    : [ntrain, ntrain + nval - 1],
                           'test'   : [ntrain + nval, nimages - 1]}  # start & end inclusive
                datasets.append(dataset)
    return datasets


### Generate the data files (with all the depth, flow etc.) for each sequence
def generate_baxter_sequence(dataset, id):
    # Get stuff from the dataset
    path, step, seq, suffix = dataset['path'], dataset['step'], dataset['seq'], dataset['suffix']
    # Setup start/end IDs of the sequence
    start, end = id, id + (step * seq)
    sequence, ct, stepid = {}, 0, step
    for k in xrange(start, end + 1, step):
        sequence[ct] = {'depth'     : path + 'depth' + suffix + str(k) + '.png',
                        'label'     : path + 'labels' + suffix + str(k) + '.png',
                        'state1'    : path + 'state' + str(k) + '.txt',
                        'state2'    : path + 'state' + str(k + 1) + '.txt',
                        'se3state1' : path + 'se3state' + str(k) + '.txt',
                        'se3state2' : path + 'se3state' + str(k + 1) + '.txt',
                        'flow': path + 'flow_' + str(stepid) + '/flow' + suffix + str(start) + '.png'}
        stepid += step  # Get flow from start image to the next step
        ct += 1  # Increment counter
    return sequence


############
### DATA LOADERS: FUNCTION TO LOAD DATA FROM DISK & TORCH DATASET CLASS

### Load baxter sequence from disk
def read_baxter_sequence_from_disk(dataset, id, options):
    # Setup memory
    sequence = generate_baxter_sequence(dataset, id)  # Get the file paths
    depths      = torch.FloatTensor(options['seqLength'] + 1, 1, options['imgHeight'], options['imgWidth'])
    labels      = torch.ByteTensor( options['seqLength'] + 1, 1, options['imgHeight'], options['imgWidth'])
    gtfwdflows  = torch.FloatTensor(options['seqLength'],     3, options['imgHeight'], options['imgWidth'])
    actconfigs  = torch.FloatTensor(options['seqLength'] + 1, 7)
    comconfigs  = torch.FloatTensor(options['seqLength'] + 1, 7)
    controls    = torch.FloatTensor(options['seqLength'], options['nCtrl'])
    poses       = torch.FloatTensor(options['seqLength'] + 1, options['meshIds'].nelement() + 1, 3, 4).zero_()

    # Load sequence
    dt = options['stepLength'] * (1.0 / 30.0)
    for k in xrange(len(sequence)):
        # Get data table
        s = sequence[k]

        # Load depth & label
        depths[k] = read_depth_image(s['depth'], options['imgHeight'], options['imgWidth'], options['imgScale'])
        labels[k] = torch.ByteTensor(cv2.imread(s['label'], -1))

        # Load configs
        state = read_baxter_state_file(s['state1'])
        actconfigs[k] = state['actjtpos']
        comconfigs[k] = state['comjtpos']

        # Load SE3 state
        se3state = read_baxter_se3state_file(s['se3state1'])
        poses[k, 0, :, 0:3] = torch.eye(3).float()  # Identity transform for BG
        for j in xrange(options['meshIds'].nelement()):
            meshid = options['meshIds'][j]
            se3tfm = options['cameraData']['modelView'] * se3state[meshid]  # Camera data is part of options
            poses[k][j + 1] = se3tfm[0:3, :]  # 3 x 4 transform

        # Load controls and FWD flows (for the first "N" items)
        if k < options['seqLength']:
            # Load flow
            gtfwdflows[k] = read_flow_image_xyz(s['flow'], options['imgHeight'], options['imgWidth'],
                                                options['imgScale'])

            # Load controls
            if options['baxterCtrlType'] == 'comvel':  # Right arm joint velocities
                controls[k] = state['comjtvel']
            elif options['baxterCtrlType'] == 'actvel':
                controls[k] = state['actjtvel']
            elif options['baxterCtrlType'] == 'comacc':  # Right arm joint accelerations
                controls[k] = state['comjtacc']
            elif options['baxterCtrlType'] == 'comboth':
                controls[k][0:7] = state['comjtvel']  # 0-6  = Joint velocities
                controls[k][7:14] = state['comjtacc']  # 7-13 = Joint accelerations

    # Different control types
    if options['baxterCtrlType'] == 'actdiffvel':
        controls = (actconfigs[1:options['seqLength'] + 1, :] - actconfigs[0:options['seqLength'], :]) / dt
    elif options['baxterCtrlType'] == 'comdiffvel':
        controls = (comconfigs[1:options['seqLength'] + 1, :] - comconfigs[0:options['seqLength'], :]) / dt

    # Return loaded data
    data = {'depths': depths, 'labels': labels, 'gtfwdflows': gtfwdflows, 'controls': controls,
            'actconfigs': actconfigs, 'comconfigs': comconfigs, 'poses': poses}
    return data


### Dataset for Baxter Sequences
class BaxterSeqDataset(Dataset):
    ''' Datasets for training SE3-Nets based on Baxter Sequential data '''

    def __init__(self, datasets, load_function, dtype='train'):
        '''
        Create the data loader given paths to existing list of datasets:
        :param datasets: 		List of datasets that have train | test | val splits
        :param load_function:	Function for reading data from disk given a dataset and an ID (this function needs to
                                return a dictionary of torch tensors)
        :param dtype:			Type of dataset: 'train', 'test' or 'val'
        '''
        assert (len(datasets) > 0);  # Need atleast one dataset
        assert (dtype == 'train' or dtype == 'val' or dtype == 'test')  # Has to be one of the types
        self.datasets = datasets
        self.load_function = load_function
        self.dtype = dtype

        # Get some stats
        self.numdata = 0
        self.datahist = [0]
        for d in self.datasets:
            numcurrdata = int(d[self.dtype][1] - d[self.dtype][0] + 1)
            self.numdata += numcurrdata
            self.datahist.append(self.datahist[-1] + numcurrdata)
        print('Setting up {} dataset. Total number of data samples: {}'.format(self.dtype, self.numdata))

    def __len__(self):
        return self.numdata

    def __getitem__(self, idx):
        # Find which dataset to sample from
        assert (idx < self.numdata);  # Check if we are within limits
        did = np.digitize(idx,
                          self.datahist) - 1  # If [0, 10, 20] & we get 10, this will be bin 2 (10-20), so we reduce by 1 to get ID

        # Find ID of sample in that dataset (not the same as idx as we might have multiple datasets)
        start = self.datasets[did][self.dtype][
            0]  # This is the ID of the starting sample of the train/test/val part in the entire dataset
        diff = (idx - self.datahist[did])  # This will be from 0 - size for either train/test/val part of that dataset
        sid = int(start + diff)

        # Call the disk load function
        # Assumption: This function returns a dict of torch tensors
        sample = self.load_function(self.datasets[did], sid)

        # Return
        return sample