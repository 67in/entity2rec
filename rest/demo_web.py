import sys
sys.path.append('.')
from entity2rec.sparql import Sparql
import time
import pickle
from collections import defaultdict, Counter
import heapq
import logging
from flask import Flask
from flask import request
import json
from pymongo import MongoClient
import random
from flask_cors import CORS
import numpy as np
import os
import datetime


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

start_time = time.time()

version_api = '0.1'

dataset = 'LibraryThing'

item_type = 'book'

testing = True

mongodb_port = 27027

app_port = 5888

connection = MongoClient('localhost', mongodb_port)
entity2rec = connection.entity2rec
feedback_collection = entity2rec.feedback
seed_collection = entity2rec.seed
discard_collection = entity2rec.discard


@app.before_first_request
def load_model():

    print('loading model')

    # open item to item similarity matrix and read into dictionary
    with open('datasets/'+dataset+'/item_to_item_similarity_Entity2Rec', 'rb') as f1:
        global item_to_item_similarity_dict_entity2rec
        item_to_item_similarity_dict_entity2rec = pickle.load(f1)  # seed -> {item: score}

    if testing:

        # open item to item similarity matrix and read into dictionary
        with open('datasets/'+dataset+'/item_to_item_similarity_ItemKNN', 'rb') as f2:
            global item_to_item_similarity_dict_itemknn
            item_to_item_similarity_dict_itemknn = pickle.load(f2)  # seed -> {item: score}


@app.before_first_request
def read_item_metadata():

    # reads list of item in the dataset
    global items_all
    items_all = set()

    # item popularity
    global pop_dict
    pop_dict = Counter()

    with open('datasets/'+dataset+'/all.dat') as all_ratings:

        for line in all_ratings:
            
            line_split = line.strip('\n').split(' ')

            item = line_split[1]

            pop_dict[item]+=1

            items_all.add(item)

    # reads items metadata from sparql endpoint and keeps them in memory
    global item_metadata
    item_metadata = {}

    # check whether a thumbnail index file exists and is not empty
    filepath = 'datasets/'+dataset+'/thumbnails.txt'

    if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:

        thumbnail_index_file = open(filepath)

        thumbnail = {}

        for line in thumbnail_index_file:

            line_split = line.strip('\n').split(' ')

            item = line_split[0]
            thumb = line_split[1]

            thumbnail[item] = thumb

        thumbnail_exists = True

    # if it does not exist, we need to create it
    else:

        thumbnail_exists = False

        thumbnail_index_file = open(filepath, 'w')

    for item in items_all:

        metadata = Sparql.get_item_metadata(item, item_type, thumbnail_exists)

        if metadata:  # skip items with missing metadata

            # thumbnail has been scraped and is already in metadata
            if not thumbnail_exists:

                thumb = metadata['thumbnail']

                # write on thumbnail index file
                thumbnail_index_file.write('%s %s\n' %(item, thumb))

            # I can retrieve the thumbnail from the dictionary
            else:

                try:

                    metadata['thumbnail'] = thumbnail[item]

                except KeyError:
                    logger.info("%s removed - no thumbnail in index\n" %item)
                    del pop_dict[item]
                    continue

            item_metadata[item] = metadata

            logger.info("%s\n" %item)

        else:  # remove items from popularity dictionary
            logger.info("%s removed\n" %item)
            del pop_dict[item]

    # close thumbnail index file
    thumbnail_index_file.close()

    # probs from popularity dictionary
    global probs
    probs = []
    global items
    items = []
    tot_sum = sum(pop_dict.values())

    for key, value in pop_dict.items():

        items.append(key)
        probs.append(value/tot_sum)

    # use temperature to speed up onboarding

    temperature = 0.3
    # helper function to sample an index from a probability array
    probs = np.asarray(probs).astype('float64')
    probs = probs ** (1 / temperature)
    probs /= np.sum(probs)

    global num_items
    num_items = len(item_metadata)

    assert num_items == len(items)


@app.route('/entity2rec/' + version_api + "/onboarding", methods=['GET'])
def onboarding():

    out = {}

    out['user_id'] = time.time()  # FIXME

    global item_to_item_similarity_dict
    global algorithm
    global discarded_items
    discarded_items = []

    if testing:
        # A/B testing
        if random.random() >= 0.5:
            item_to_item_similarity_dict = item_to_item_similarity_dict_entity2rec
            algorithm = 'entity2rec'

        else:
            item_to_item_similarity_dict = item_to_item_similarity_dict_itemknn
            algorithm = 'itemknn'

    else:
        item_to_item_similarity_dict = item_to_item_similarity_dict_entity2rec
        algorithm = 'entity2rec'

    number_of_samples = 100

    if num_items < number_of_samples:

        number_of_samples = num_items

    for sampled_item in np.random.choice(items, number_of_samples, p=probs, replace=False):

        out[sampled_item] = item_metadata[sampled_item]

    out_json = json.dumps(out, indent=4)

    return out_json


@app.route('/entity2rec/' + version_api + "/recs", methods=['POST'])
def recommend():

    logger.info("Launch of the entity2rec recommendation REST API")
    content = request.get_json(silent=True)

    seed = None
    N = 5

    try:
        seed=content['seed']
        user_id=content['user_id']
    except KeyError:
        raise ValueError('Please provide a seed item and a user_id.')

    seed_collection.save(content)

    rec_time = time.time()

    # remove seed from candidate items

    candidates = []

    for candidate in item_metadata.keys():

        if candidate != seed and candidate not in discarded_items:

            candidates.append(candidate)

    # retrieve similarity values for the seed item

    d = item_to_item_similarity_dict_entity2rec[seed]

    recs = heapq.nlargest(N, candidates, key=lambda x: d[x])

    out = {}

    out['recs'] = []

    for r in recs:

        out['recs'].append({r: item_metadata[r]})

    out['user_id'] = user_id

    out_json = json.dumps(out, indent=4, sort_keys=True)

    logger.info('total rec time')
    logger.info("--- %s seconds ---" % (time.time() - rec_time))

    return out_json


@app.route('/entity2rec/' + version_api + "/feedback", methods=['POST'])
def feedback():

    content = request.get_json(silent=True)

    try:
        uri=content['uri']
        user_id=content['user_id']
        feedback=content['feedback']
        position=content['position']

    except KeyError:
        raise ValueError('Please provide a uri, user_id,feedback and position of the item.')

    content['timestamp'] = datetime.datetime.fromtimestamp(time.time()).strftime('%d-%m-%Y %H:%M:%S')

    content['date'] = content['timestamp'].split(' ')[0]

    content['algorithm'] = algorithm

    feedback_collection.save(content)

    return 'ok\n'


@app.route('/entity2rec/' + version_api + "/discard", methods=['POST'])
def discard():

    content = request.get_json(silent=True)

    try:
        seed=content['seed']
        user_id=content['user_id']
    except KeyError:
        raise ValueError('Please provide a seed item and a user_id.')

    discard_collection.save(content)

    discarded_items.append(seed)

    return 'ok\n'


if __name__ == '__main__':

    app.run(host='0.0.0.0', port=app_port, debug=True)
