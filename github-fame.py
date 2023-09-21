#! usr/bin/env python3

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import http.client
import json
import pathlib
import queue
import re
import sys
from threading import Thread
import time
from typing import Optional, Iterable, Callable
import unidiff
import urllib.error
from urllib.parse import urlparse, parse_qs
import urllib.request


GITHUB_TOKEN = ""


@dataclass
class PullRequest:
    id: int
    merged: bool
    author: str
    title: str
    api_url: str
    changes: Optional[unidiff.PatchSet] = None


@dataclass
class UserStatistics:
    pull_requests: int = 0
    additions: int = 0
    deletions: int = 0
    files_touched: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def response_for_api_path(uri: str, content_type: str = "application/vnd.github+json") -> http.client.HTTPResponse:
    request = urllib.request.Request(uri)
    request.add_header("Accept", content_type)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if GITHUB_TOKEN:
        request.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")

    while True:
        try:
            return urllib.request.urlopen(request)
        except urllib.error.HTTPError as e:
            if e.code == 403:  # rate limit exceeded
                sleep_time = max(0.3, int(e.headers["x-ratelimit-reset"]) - time.time() + 0.1)
                print(f"Hit rate limit of {e.headers['x-ratelimit-limit']} requests. "
                      + f"Sleeping for {sleep_time:.2f} seconds"
                      # + f" (until {e.headers['x-ratelimit-reset']}, current time {time.time()})"
                      + ". Use authorization to prevent this.",
                      file=sys.stderr)
                time.sleep(sleep_time)
                continue
            raise


def collect_paginated_json_results(base_uri: str, response_to_result_items: Callable[[object], list]):
    response = response_for_api_path(base_uri)

    # https://docs.github.com/en/rest/guides/using-pagination-in-the-rest-api?apiVersion=2022-11-28
    last_page = 1
    if "Link" in response.headers:
        link_headers = {match[1]: match[0] for match in re.findall(r"\<(.+?)\>; rel=\"(prev|next|last|first)\"", response.headers["Link"])}
        last_uri = link_headers["last"]
        last_page = int(parse_qs(urlparse(last_uri).query)["page"][0])

    print(f"Collecting paginated result, requires {last_page - 1} more requests: ")

    results = []
    response_data = json.loads(response.read())
    results.extend(response_to_result_items(response_data))

    for page in range(2, last_page + 1):
        response = response_for_api_path(base_uri + f"&page={page}")
        results.extend(response_to_result_items(json.loads(response.read())))
        print(".", end="", flush=True)
    
    print(" Done")

    return results


def get_pull_requests_using_search(repo: str, filter_author: Optional[str] = None) -> list[PullRequest]:
    def get_search_link(per_page: int = 100):
        uri = f"https://api.github.com/search/issues?per_page={per_page}&q=is:pr+repo:{repo}"
        if filter_author:
            uri += f"+author:{filter_author}"
        return uri

    probe_result_count_response = urllib.request.urlopen(get_search_link(1))
    result_count = json.loads(probe_result_count_response.read())["total_count"]
    if result_count > 1000:
        raise RuntimeError(f"GitHub reported {result_count} results, but the search API will only retrieve the first 1000 results.")

    def response_to_result_items(json_object: object) -> list[PullRequest]:
        return [PullRequest(
                id=search_result_object["number"],
                merged=search_result_object["pull_request"]["merged_at"] != None,
                author=search_result_object["user"]["login"],
                title=search_result_object["title"],
                api_url=search_result_object["pull_request"]["url"]
            ) for search_result_object in json_object["items"]
        ]
    
    print(f"Getting pull requests for {repo} using GitHub's search API")
    pull_request_list = collect_paginated_json_results(get_search_link(), response_to_result_items)
    pull_requests_by_id = {pr.id: pr for pr in pull_request_list}   # to ensure no duplicates due to new PRs while traversing pages
    return list(pull_requests_by_id.values())


def get_pull_requests_using_pulls(repo: str) -> list[PullRequest]:
    uri = f"https://api.github.com/repos/{repo}/pulls?state=all&per_page=100"

    def response_to_result_items(json_object: object) -> list[PullRequest]:
        return [PullRequest(
                id=search_result_object["number"],
                merged=search_result_object["merged_at"] != None,
                author=search_result_object["user"]["login"],
                title=search_result_object["title"],
                api_url=search_result_object["url"]
            ) for search_result_object in json_object
        ]

    print(f"Getting pull requests for {repo} using GitHub's pulls API")
    pull_request_list = collect_paginated_json_results(uri, response_to_result_items)
    pull_requests_by_id = {pr.id: pr for pr in pull_request_list}   # to ensure no duplicates due to new PRs while traversing pages
    return list(pull_requests_by_id.values())


def annotate_changes(pull_request: PullRequest):
    diff_response = response_for_api_path(pull_request.api_url, content_type="application/vnd.github.diff")
    diff_response_encoding = diff_response.headers.get_charsets()[0]
    pull_request.changes = unidiff.PatchSet(diff_response, encoding=diff_response_encoding)


def annotate_changes_parallel(pull_requests: Iterable[PullRequest], num_threads: int) -> None:
    print(f"Getting changes for {len(pull_requests)} pull requests using {num_threads} parallel connections:")

    pull_request_queue = queue.Queue()
    for pull_request in pull_requests:
        pull_request_queue.put(pull_request)

    def thread_function():
        while True:
            try:
                pr = pull_request_queue.get(block=False)
            except queue.Empty:
                return
            annotate_changes(pr)
            print(".", end="", flush=True)
            pull_request_queue.task_done()
    
    for _ in range(num_threads):
        Thread(target=thread_function, daemon=True).start()

    # not immediately using .join() as it blocks Ctrl+C / KeyboardInterrupt from exiting the program.
    while not pull_request_queue.empty():
        time.sleep(0.1)
    pull_request_queue.join()
    print("\nDone\n")


def build_statistics_per_user(pull_requests: Iterable[PullRequest], exclude_globs: list[str]) -> dict[str, UserStatistics]:
    user_statistics: dict[str, UserStatistics] = defaultdict(UserStatistics)
    for pull_request in pull_requests:
        if args.verbose:
            print(f"\nChecking #{pull_request.id} by {pull_request.author} ('{pull_request.title}')")

        user_statistics[pull_request.author].pull_requests += 1

        for patched_file in pull_request.changes:
            path = pathlib.PurePath(patched_file.path.strip('"'))
            excluded = any(path.match(glob) for glob in exclude_globs)

            if excluded:
                if args.verbose:
                    print(f"Ignoring {path} (+{patched_file.added}, -{patched_file.removed})")
                continue
        
            if args.verbose:
                print(f"Counting {path} (+{patched_file.added}, -{patched_file.removed})")

            # This handles renamed files correctly (as the diff doesn't show changes, just "rename from X" and "rename to Y")
            user_statistics[pull_request.author].additions += patched_file.added
            user_statistics[pull_request.author].deletions += patched_file.removed
            user_statistics[pull_request.author].files_touched[path] += patched_file.added + patched_file.removed

    return user_statistics


DEFAULT_EXCLUDE_GLOBS = [
    "package-lock.json",
    "*min.js",
    "*min.css",
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize contributions based on GitHub pull requests")
    parser.add_argument("repository", help="GitHub repo, in the form 'user/repo'")
    parser.add_argument("-a", "--filter-author", help="include only pull requests created by this user")
    parser.add_argument("-d", "--disable-default-exclude-globs", help=f"do not apply the default exclusion globs ({DEFAULT_EXCLUDE_GLOBS})", action="store_true")
    parser.add_argument("-e", "--exclude-glob", nargs="*", action="extend", help="add a glob for files to exclude")
    parser.add_argument("-v", "--verbose", action="store_true", help="show detailed information about what changes are included")
    parser.add_argument("-n", "--num-parallel-requests", default=10, help="number of parallel requests to retrieve pull request changes")
    parser.add_argument("-t", "--token", help="GitHub API token to use for authorization. Use for more relaxed rate limiting")
    parser.add_argument("--include-unmerged", action="store_true", help="include unmerged pull requests")
    args = parser.parse_args()

    # TODO: add flag to exclude specific PRs (e.g. because they introduced automatic code formatting)
    # TODO: Name ok? Veröffentlichen?
    
    # TODO: Initial requests in parallel (parse maximum page, go through pages)
    # TODO: Exclude globs can't use ** -- use regex instead? Or just cope with it? (fixed in python3.13)
    # TODO: File stats: Separate additions and deletions, but still sort by sum
    # TODO: File stats: show total number of files touched in "top 5" line
    # TODO: Maybe get rid of unidiff dependency?

    if args.token:
        GITHUB_TOKEN = args.token

    exclude_globs = [*args.exclude_glob]
    if not args.disable_default_exclude_globs:
        exclude_globs.extend(DEFAULT_EXCLUDE_GLOBS)
    if args.verbose:
        print(f"Using exclude globs {exclude_globs}\n")

    if args.filter_author:
        pull_requests = get_pull_requests_using_search(args.repository, args.filter_author)
    else:
        pull_requests = get_pull_requests_using_pulls(args.repository)

    if args.verbose:
        print(f"Found {len(pull_requests)} PRs: {[pr.id for pr in pull_requests]}\n")

    if not args.include_unmerged:
        if args.verbose:
            for pull_request in pull_requests:
                if not pull_request.merged:
                    print(f"Ignoring (unmerged) #{pull_request.id} by {pull_request.author} ('{pull_request.title}')")
        else:
            unmerged_count = sum(1 for pr in pull_requests if not pr.merged)
            print(f"Ignoring {unmerged_count} unmerged pull requests")
        pull_requests = [pr for pr in pull_requests if pr.merged]
        print("")
    
    annotate_changes_parallel(pull_requests, args.num_parallel_requests)
    user_statistics = build_statistics_per_user(pull_requests, exclude_globs)

    for (user, stats) in sorted(user_statistics.items()):
        print("")
        print(f"{user}: {stats.pull_requests} PRs. "
              + f"Total changes: (+{stats.additions}, -{stats.deletions}). "
              + f"Average per PR: (+{(stats.additions / stats.pull_requests):.1f}, -{(stats.deletions / stats.pull_requests):.1f})")
        
        sorted_change_pairs = sorted(stats.files_touched.items(), key=lambda pair: (-pair[1], pair[0]))
        if args.verbose:
            print("Files changed:")
        else:
            print("Top 5 files changed:")
            sorted_change_pairs = sorted_change_pairs[0:5]

        for (path, change_count) in sorted_change_pairs:
            print(f"    {path} (+- {change_count})")