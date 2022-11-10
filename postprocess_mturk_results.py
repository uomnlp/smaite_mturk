import json
import os.path
import random
import sys
from itertools import groupby
from typing import List, Dict

import boto3
import click
import pandas as pd
from loguru import logger
from tqdm import tqdm
import requests
from unidecode import unidecode
from urllib3.exceptions import RequestError


def maybe_int(i):
    try:
        return int(i)
    except:
        return i


def postprocess_answer(answers, *fields):
    return {
        f: next(maybe_int(k) for k, v in answers[f].items() if v) for f in fields
    }


#AWSAccessKeyId='AKIA5RDHGZUGXNRL776R'
#AWSSecretKey='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'

#AWS_ACCESS_KEY_ID = AWSAccessKeyId #os.environ.get('AWS_ACCESS_KEY_ID')
#AWS_SECRET_ACCESS_KEY = AWSSecretKey #os.environ.get('AWS_SECRET_ACCESS_KEY')

AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')

@click.command()
@click.argument('infile', default=sys.stdin)
@click.option('--log-level', default='INFO')
@click.option('--answer-fields', '-af', default='rate,verdict')
@click.option('--qualified-workers-file', '-qwf', type=click.Path(file_okay=True, dir_okay=False),
              default='workers.txt')
@click.option("--processed-assignments", '-pa', type=click.Path(file_okay=True, dir_okay=False),
              default='processed.json')
@click.option('--qualification-threshold', '-qt', type=float, default=.5)
@click.option('--release', is_flag=True, default=False)
@click.option('--approve', is_flag=True, default=False)
@click.option('--qualify', is_flag=True, default=False)
@click.option('--save-workers', is_flag=True, default=False)
@click.option('--dry', is_flag=True, default=False)
@click.option('--qualification-id', type=str, default='307M1J5IK9LLYXT6DC6297RO8PMME8')  # uom-factchecker (smaite)
def main(infile, log_level, answer_fields, qualified_workers_file, processed_assignments, qualification_threshold,
         release, approve, qualify, qualification_id, save_workers, dry):
    endpoint_url = 'https://mturk-requester-sandbox.us-east-1.amazonaws.com' if not release else 'https://mturk-requester.us-east-1.amazonaws.com'

    assert AWS_ACCESS_KEY_ID, AWS_ACCESS_KEY_ID
    assert AWS_SECRET_ACCESS_KEY, AWS_SECRET_ACCESS_KEY

    logger.remove(0)
    logger.add(sys.stderr, level=log_level)
    logger.debug(infile)

    #answer_fields = answer_fields.split(',')
    results: List[Dict] = [json.loads(s) for s in
                           pd.read_csv(infile).to_json(lines=True, orient='records').splitlines()]

    def key(entry):
        return tuple(entry[k] for k in entry.keys() if k.startswith('Input'))

    input_fields = tuple(k.replace("Input.", '') for k in results[0].keys() if k.startswith('Input'))
    answer_fields = tuple(k for k in json.loads(results[0]['Answer.taskAnswers'])[0].keys())
    grouped_by_input = groupby(sorted(results, key=key), key=key)
    grouped_data = []

    for i, (key, group) in enumerate(grouped_by_input):
        result = dict(zip(input_fields, key))
        for i in range(1,11):
            result[f'corrupt{i}'] = bool(result[f'corrupt{i}'])
        result['answers'] = []
        for item in group:
            worker_answers = postprocess_answer(json.loads(item['Answer.taskAnswers'])[0], *answer_fields)
            worker_answers['worker_id'] = item['WorkerId']
            result['answers'].append(worker_answers)
            grouped_data.append(result)
        #print(result)

    if os.path.exists(qualified_workers_file):
        with open(qualified_workers_file, 'r') as f:
            already_qualified_workers = f.read().splitlines()
    else:
        logger.debug("File doesn't exist!")
        already_qualified_workers = []
    logger.debug(already_qualified_workers)
    # get average worker accuracy on corrupted data
    worker_key = lambda x: x['WorkerId']
    grouped_by_worker = groupby(sorted(results, key=worker_key), key=worker_key)
    worker_acc_corrupted = {}
    worker_acc_uncorrupted = {}
    pending_workers = []  # those that didn't have any corrupted examples but we don't want to disqualify them
    for w, group in grouped_by_worker:
        group = list(group)
        all_corrupted = [
            postprocess_answer(json.loads(item['Answer.taskAnswers'])[0], *answer_fields)[f'rate{index}'] for index in range(1,11) for item
        in group if bool(item[f'Input.corrupt{index}'])
        ]
        all_uncorrupted = [
            postprocess_answer(json.loads(item['Answer.taskAnswers'])[0], *answer_fields)[f'rate{index}'] for index in range(1,11) for item
        in group if not bool(item[f'Input.corrupt{index}'])
        ]

        logger.debug(all_corrupted)
        logger.debug(all_uncorrupted)
        if all_corrupted and all_uncorrupted:
            worker_acc_corrupted[w] = sum(all_corrupted) / len(all_corrupted) / 10
            worker_acc_uncorrupted[w] = sum(all_uncorrupted) / len(all_uncorrupted) /10
            logger.debug(
                f"{w} had {len(all_corrupted)} corrupted questions. {worker_acc_corrupted[w]} rating (vs {qualification_threshold}).")
        else:
            pending_workers.append(w)
            logger.debug(f"{w} is pending!")

    # qualify those where avg score on corrupted is lower than threshold and at least 0.5 lower than on uncorrupted.
    qualified_workers = [k for k, v in worker_acc_corrupted.items() if
                         v < qualification_threshold and (v + 0.5) < worker_acc_uncorrupted[k]]
    # disqualify non-qualified and non-pending workers
    disqualified_workers = [k for k, v in worker_acc_corrupted.items() if
                            k not in qualified_workers and k not in pending_workers]
    mturk = boto3.client(
        'mturk',
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name='us-east-1',
        endpoint_url=endpoint_url
    )

    if os.path.exists(processed_assignments):
        with open(processed_assignments, 'r') as f:
            already_processed = json.load(f)
    else:
        already_processed = []

    if approve:
        # reject all disqualified workers and do not grant qualification, approve otherwise
        logger.info("Approving hits")
        for d in tqdm(results):  # approve/reject
            if d['AssignmentStatus'] == 'Submitted':
                if d['AssignmentId'] not in already_processed:
                    try:
                        if not dry:
                            mturk.approve_assignment(AssignmentId=d['AssignmentId'], RequesterFeedback='thank you!')
                        logger.debug(f"Approving {d['AssignmentId']} ({'live' if not dry else 'dry'})")
                    except RequestError as e:
                        if "This operation can be called with a status of: Submitted" in str(e):
                            logger.debug(f"{d['AssignmentId']} was already approved!")
                    already_processed.append(d['AssignmentId'])
                else:
                    logger.debug(f"{d['AssignmentId']} was already processed!")

    if qualify:
        # approve all qualified and grant qualification == 100*worker_acc
        logger.info("Qualifying workers")
        for w in tqdm(qualified_workers):  # grant qualification
            acc = worker_acc_corrupted[w]

            if not dry:
                mturk.associate_qualification_with_worker(QualificationTypeId=qualification_id, WorkerId=w,
                                                          IntegerValue=int(acc * 100))
            logger.debug(f"Qualifying {w} with {int(acc * 100)}! ({'live' if not dry else 'dry'})")
    
    if save_workers:
        for w in qualified_workers:
            if w in already_qualified_workers:
                logger.warning(f"{w} is already qualified!")
        qualified_workers = [w for w in qualified_workers if w not in already_qualified_workers]
        with open(qualified_workers_file, 'a+') as f:
            f.write('\n'.join(qualified_workers) if qualified_workers else '')

    # TODO: get average worker disagreement



if __name__ == '__main__':
    main()