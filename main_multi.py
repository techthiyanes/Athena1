# the codebase for Athena is based on https://github.com/microsoft/CodeBERT/
import os
import torch
import copy
import argparse

from data import SoftwareRepo
import extractor
import pandas as pd
import numpy as np

from pathlib import Path
from tqdm import tqdm
from git import Git, Repo
from collections import defaultdict
from scipy.spatial.distance import cdist
from parser import remove_comments_and_docstrings
from typing import List, Dict, Any, Set, Optional

import multiprocessing
       

def clone_repo(dataset_csv, project_path):
    for _, row in dataset_csv.iterrows():
        repo_path = Path(project_path) / row['repo']
        if not repo_path.exists():
            try:
                repo_path.mkdir(parents = True)
                Repo.clone_from (row['github_link'].split('/commit/')[0], str(repo_path))
            except Exception as e:
                print (e)


def search_query(file_path, method_name, method_df, lang):
    overload_idxes = []
    idxes = method_df.index[
        method_df.path == str(file_path)
    ].tolist()  
    for idx in idxes:
        mtd = remove_comments_and_docstrings(method_df.method[idx],lang).split('\n')
        for i in range(len(mtd)):
            if mtd[i].lstrip().startswith('@') or not mtd[i]:
                continue
            if '(' not in mtd[i]:
                mtd[i] += mtd[i+1]
            if method_name == mtd[i].split('(')[0].split()[-1].split('*/')[-1]:
                overload_idxes.append (idx)
            break 
    return overload_idxes, idxes


def get_degree (adj):
    degree = np.sum (adj, axis = 1)
    for i in range(len(degree)):
        if degree[i]:
            degree[i] = 1 / np.sqrt(degree[i])  
    return degree


def get_noralimized_adjacency(src_nodes, trgt_nodes, class_src_nodes, class_trgt_nodes, corpus_size, weight, nebr_num):
    adj = np.zeros ([corpus_size, corpus_size])
    adj_sec = np.zeros ([corpus_size, corpus_size])
    for i in range(corpus_size):
        direct_nebr, second_nebr = set(), set()
        for j in range(len(src_nodes)):
            if i == src_nodes[j]:
                adj[i][trgt_nodes[j]] = 1
                direct_nebr.add(trgt_nodes[j])
            if i == trgt_nodes[j]:
                adj[i][src_nodes[j]] = 1  
                direct_nebr.add(src_nodes[j])                  
        for j in range(len(src_nodes)):
            for k in direct_nebr:
                if k == src_nodes[j] and not adj[i][trgt_nodes[j]]:
                    adj_sec[i][trgt_nodes[j]] = 1
                    second_nebr.add(trgt_nodes[j])
                if k == trgt_nodes[j] and not adj[i][src_nodes[j]]:
                    adj_sec[i][src_nodes[j]] = 1
                    second_nebr.add(src_nodes[j])
        adj[i][i] = 0
        adj_sec[i][i] = 0

    adj_class = np.zeros ([corpus_size, corpus_size])
    for i in range(corpus_size):
        for j in range(len(class_src_nodes)):
            if i == class_src_nodes[j]:
                adj_class[i][class_trgt_nodes[j]] = 1
            if i == class_trgt_nodes[j]:
                adj_class[i][class_src_nodes[j]] = 1                 
        adj_class[i][i] = 0

    adj += adj_class
    degree, degree_sec = get_degree(adj), get_degree(adj_sec)
    adj = np.matmul(np.matmul(np.diag(degree), adj * weight), np.diag(degree)) + np.identity(corpus_size)
    adj_sec = adj + np.matmul(np.matmul(np.diag(degree_sec), adj_sec * weight), np.diag(degree_sec)) 
    return adj, adj_sec


def calculate_metric(all_distances, distances):
    query_size = all_distances.shape[0]   
    ranks = np.zeros(distances.shape)
    for i in range(query_size):
        rank = []
        grd_truth = np.expand_dims (distances[:, i], axis = -1)
        rank = np.sum (all_distances <= grd_truth, axis = -1)
        ranks[:, i] = rank

    ranks [ranks == 0] = 1
    sort_ranks = np.sort(ranks, kind = 'mergesort')
    grd_truth_size = np.sum (sort_ranks < all_distances.shape[1], axis = -1)

    rank = sort_ranks[:,0]
    rr = 1.0 / rank
    avep, hit_10 = np.zeros(query_size), np.zeros(query_size)
    for i in range(query_size):
        if rank[i] <= 10:
            hit_10[i] = 1
        p_list = []
        for j in range(query_size):
            if sort_ranks[i][j] == all_distances.shape[1]:
                break
            p_list.append((j+1)/sort_ranks[i][j])
            avep[i] = np.mean(p_list) if p_list else 0.0
    return rank, rr, avep, hit_10, sort_ranks, grd_truth_size


def generate_results(item):    
    repo, parent_commit, path_lines, repo_cg, corpus_vecs, args = item
    results = [[] for i in range(args.nebr_order)]
    src_nodes = repo_cg.call_edge_df.from_id.values
    trgt_nodes = repo_cg.call_edge_df.to_id.values
    class_src_nodes = repo_cg.class_edge_df.from_id.values
    class_trgt_nodes = repo_cg.class_edge_df.to_id.values
    
    # store indexes of query methods. Two-dimensional list to handle overloaded methods
    query_overload_idxes, file_idxes, method_path = [], [], []
    for path_line in path_lines:
        path = path_line.split('<sep>')
        overload_idxes, idxes = search_query (Path(args.project_path) / repo / path[0], path[1], repo_cg.method_df, args.lang)
        if overload_idxes:
            query_overload_idxes.append(overload_idxes)
            file_idxes.append(idxes)
            method_path.append(os.path.join(path[0], path[1]))
   
    query_size, corpus_size = len(query_overload_idxes), len(corpus_vecs)                                                 
    adj, adj_sec = get_noralimized_adjacency(src_nodes, trgt_nodes, class_src_nodes, class_trgt_nodes, corpus_size, args.weight, args.nebr_num)                                                  
    for level in range(args.nebr_order):
        corpus_vecs_w = corpus_vecs
        if level // 3 == 1:
            corpus_vecs_w = np.matmul(adj, corpus_vecs) 
        elif level // 3 == 2:    
            corpus_vecs_w = np.matmul(adj_sec, corpus_vecs)  

        query_vecs = np.zeros ([query_size, corpus_vecs_w.shape[1]])              
        for i in range(query_size):
            query_vecs[i] = corpus_vecs_w[query_overload_idxes[i][0]] 

        all_distances = cdist (query_vecs, corpus_vecs_w, metric = 'cosine')                
        distances = np.zeros ([query_size, query_size])
        for i in range(query_size):                          
            # set the distance between the query and itself to MAX value
            for idx in query_overload_idxes[i]:
                all_distances[i][idx] = args.MAX
            if level % 3 == 1:
                for idx in range(corpus_size):
                    if idx not in file_idxes[i]:
                        all_distances[i][idx] = args.MAX
            elif level % 3 == 2:
                for idx in file_idxes[i]:
                    all_distances[i][idx] = args.MAX
            for j in range(query_size):
                if (len(query_overload_idxes[j]) == 1):
                    distances[i][j] = all_distances[i][query_overload_idxes[j][0]]
                else:
                    dist = []
                    for k in query_overload_idxes[j]:
                        dist.append (all_distances[i][k])
                    distances[i][j] = min (dist)
    
        # compute rank matrix (query * grd_truth)
        rank, rr, avep, hit_10, sort_ranks, grd_truth_size = calculate_metric(all_distances, distances)
        for i in range(query_size):
            if rank[i] != corpus_size:
                results[level].append({
                    "repo": repo,
                    "parent commit": parent_commit,
                    "method path": method_path[i],
                    "rank": rank[i],
                    "RR": rr[i],
                    "AP": avep[i],
                    "hit@10": hit_10[i],
                    "sort_ranks": sort_ranks[i].tolist(),
                    "ground truth size": grd_truth_size[i],
                    "inner corpus size": len(file_idxes[i]) - len(query_overload_idxes[i]),
                    "outer corpus size": all_distances.shape[1] - len(file_idxes[i]),
                    "repo size": all_distances.shape[1] 
                })                 

    return results


def main():
    import os
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'

    parser = argparse.ArgumentParser()
    # Required arguments
    parser.add_argument("--project_path", default='./athena_reproduction_package/projects', type=str,
                        help="the path of the downloaded projects by using GitHub URL from the dataset")    
    parser.add_argument("--pretrained_model_name", default='microsoft/graphcodebert-base', type=str,
                        help="The model checkpoint for weights initialization.")
    parser.add_argument("--finetuned_model_path", default='./athena_reproduction_package/finetuned_models/graphcodebert.bin', type=str,
                        help="The model checkpoint after finetuned on the code search task.")
    parser.add_argument("--lang", default='java', type=str,
                        help="The programming language for parsing")
    parser.add_argument('--output_dir', default='./athena_reproduction_package/results/graphcodebert', help='Path where to save results.')
    parser.add_argument("--weight", default=0.5, type=float,
                        help="The weight used to balance the method and its neighbor method information")
    parser.add_argument("--MAX", default=10000, type=int,
                        help="The model checkpoint for weights initialization.")
    parser.add_argument("--nebr_num", default=100, type=int,
                        help="# of third-order nebrs is taken into considerating")
    parser.add_argument("--version", default='baseline', type=str,
                        help="The version used to obtain the results: athena or baseline")
    parser.add_argument("--nebr_order", default=9, type=int,
                        help="The version used to obtain the results: athena or baseline")
    args = parser.parse_args()

    # read the dataset and clone the repositories
    dataset_csv = pd.read_csv('./dataset/alexandria.csv')
    clone_repo(dataset_csv, args.project_path)
    dataset = defaultdict(lambda: defaultdict(list))
    for _, row in dataset_csv.iterrows():  
        dataset[row['repo']][row['parent_commit']].append(
            row['file_path'] + '<sep>' + row['method_name']
        )

    # load the fine-tuned model
    if args.pretrained_model_name == 'microsoft/codebert-base':
        embed = extractor.EmbedCodebert(args.pretrained_model_name, args.finetuned_model_path)
    elif args.pretrained_model_name == 'microsoft/graphcodebert-base':
        embed = extractor.EmbedGraphcodebert(args.pretrained_model_name, args.finetuned_model_path)
    else:
        embed = extractor.EmbedUnixcoder(args.pretrained_model_name, args.finetuned_model_path)    
    embed.load_finetuned_model() 

    cg_repos = []
    for repo in tqdm(dataset):
        for parent_commit in tqdm(dataset[repo]):                               
            repo_path = Path(args.project_path) / repo
            repo_cg = SoftwareRepo(repo_path, parent_commit)
            corpus_vecs = embed.extract_corpus_vecs(repo_cg.method_df.method.values)
            cg_repos.append((repo, parent_commit, dataset[repo][parent_commit], repo_cg, corpus_vecs, args))

    cpu_cont = 16
    pool = multiprocessing.Pool(cpu_cont)
    agg_results = pool.map(generate_results, tqdm(cg_repos,total=len(cg_repos)))
    pool.close()
    pool.join()

    csv_results = [[] for i in range(args.nebr_order)]
    for row in agg_results:
        for level in range(args.nebr_order):
            csv_results[level] += row[level]
    for level in range(args.nebr_order):
        results_csv = pd.DataFrame(csv_results[level])
        write_path = os.path.join(args.output_dir, 'results_' + str(level+1) + '.csv')
        results_csv.to_csv(write_path, index=False) 


if __name__ == "__main__":
    main()