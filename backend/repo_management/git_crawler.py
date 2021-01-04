import pprint
import re
import time
from collections import defaultdict, namedtuple
from copy import copy
from pathlib import Path
from typing import Dict, List
from uuid import uuid4

import git
from bidict import bidict
from sqlalchemy.exc import OperationalError

import db_schema as db
from helpers.path_helpers import REPO_PATH
from repo_management.git_crawl_items import Commit


class CurrentFilesInfoCollector:
    """Calculates and tracks the line count of all files in the given branches."""

    MAGIC_EMPTY_HASH = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'  # See https://stackoverflow.com/a/40884093
    EXTRACT_NUMBER_AT_START_REGEX = re.compile(r'^(\d+)')
    EntryInfo = namedtuple('EntryInfo', ['id', 'path', 'line_count'])

    def __init__(self, repo: git.Repo):
        self._repo = repo
        self._file_infos_of_branch = {}

    def add_branch_info(self, branch, branch_head_hash, current_paths_of_branch):
        line_counts_of_files = self._parse_current_line_counts(branch_head_hash)
        file_infos = []
        for file_id, file_path in current_paths_of_branch.items():
            file_info = self.EntryInfo(file_id, file_path, line_counts_of_files.get(file_path, None))
            file_infos.append(file_info)

        self._file_infos_of_branch[branch] = file_infos

    def _parse_current_line_counts(self, commit_hash):
        raw_text = self._repo.git.execute(f'git diff --stat {self.MAGIC_EMPTY_HASH} {commit_hash}')
        line_counts_of_files = {}
        for raw_line in raw_text.splitlines():
            file_path, line_count = self._parse_single_git_line_count_line(raw_line)
            if file_path is not None:
                line_counts_of_files[file_path] = line_count
        return line_counts_of_files

    def _parse_single_git_line_count_line(self, line: str):
        # Schema:  myDir/subDir/myFile.txt         |    39 +
        parts = line.split('|')
        if len(parts) != 2:
            return None, None
        file_path = parts[0].strip()
        line_count_match = re.findall(self.EXTRACT_NUMBER_AT_START_REGEX, parts[1].strip())
        if not line_count_match:  # At binary entries like: picture.jpg    | Bin 0 -> 27063 bytes
            return None, None
        return file_path, int(line_count_match[0])

    def add_to_db(self, db_session):
        for branch, file_infos in self._file_infos_of_branch.items():
            for file_info in file_infos:
                sql_entry = db.SqlCurrentFileInfo(
                    branch=branch,
                    file_id=file_info.id,
                    current_path=file_info.path,
                    line_count=file_info.line_count
                )
                db_session.add(sql_entry)

    def __repr__(self):
        return pprint.pformat(self._file_infos_of_branch)

    def as_dict(self):
        return {branch: [dict(entry._asdict()) for entry in entries]
                for branch, entries in self._file_infos_of_branch.items()}


class CommitProvider:
    """ Caches existing commit info objects from the database. If a requested commit info is not found,
     it creates a new one from the given data."""
    def __init__(self):
        self._available_commits = {}

    def cache_from_db(self):
        db_session = db.get_session()
        try:
            self._available_commits = self.__fetch_all_commits_from_db(db_session)
        except OperationalError:  # No database was initialized yet
            self._available_commits = {}
        finally:
            db_session.close()

    def __fetch_all_commits_from_db(self, db_session):
        db_metadata = db_session.query(db.SqlCommitMetadata).all()
        db_affected_files = db_session.query(db.SqlAffectedFile).all()
        affected_files_of_commit = defaultdict(list)
        for db_af in db_affected_files:
            affected_files_of_commit[db_af.hash].append(db_af)

        return {db_md.hash: Commit.from_db(db_md, affected_files_of_commit[db_md.hash]) for db_md in db_metadata}

    def get_commit(self, git_commit: git.Commit) -> Commit:
        # This will always return a commit
        if git_commit.hexsha not in self._available_commits:
            self._available_commits[git_commit.hexsha] = Commit.from_git_commit(git_commit)
        return self._available_commits[git_commit.hexsha]

    def get_cached_commit(self, commit_hash) -> Commit:
        # This will only return a commit if it was already processed here.
        return self._available_commits.get(commit_hash, None)


class CommitCrawlerState:
    IDLE = 'IDLE'
    CLONE_REPO = 'CLONE_REPO'
    UPDATE_REPO = 'UPDATE_REPO'
    GET_PREVIOUS_COMMITS = 'GET_PREVIOUS_COMMITS'
    EXTRACT_COMMITS = 'EXTRACT_COMMITS'
    CALCULATE_PATHS = 'CALCULATE_PATHS'
    SAVE_TO_DB = 'SAVE_TO_DB'


class CommitCrawler:

    def __init__(self, repo_path: Path, commit_provider: CommitProvider):
        self._repo_path = Path(repo_path)
        self._commit_provider = commit_provider

        self._repo = None
        self._child_commit_graph = None
        self._latest_hashes = None

        self._commits_processed = 0
        self._commits_total = 0
        self._current_operation = CommitCrawlerState.IDLE
        self._error_message = ''

    def is_checked_out(self):
        return Path(self._repo_path, '.git').exists()

    def checkout(self, repo_url):
        git.Repo.clone_from(repo_url, self._repo_path, no_checkout=True)

    def get_crawl_status(self):
        return {
            'commits_processed': self._commits_processed,
            'commits_total': self._commits_total,
            'current_operation': self._current_operation,
            'error_message': self._error_message,
        }

    def is_busy(self):
        return self._current_operation != CommitCrawlerState.IDLE

    def crawl(self, update_before_crawl=True, limit_tracked_branches_days_last_activity=None):
        if self.is_busy():
            return

        self._error_message = ''
        try:
            result = self._crawl(update_before_crawl, limit_tracked_branches_days_last_activity)
            self._current_operation = CommitCrawlerState.SAVE_TO_DB
            result.write_to_db()
        except Exception as e:
            self._error_message = str(e)
        self._current_operation = CommitCrawlerState.IDLE

    def _crawl(self, update_before_crawl, limit_tracked_branches_days_last_activity):
        if not self.is_checked_out():
            raise FileNotFoundError('The repository has not been checked out yet.')

        self._repo = git.Repo(self._repo_path)
        if update_before_crawl:
            self._current_operation = CommitCrawlerState.UPDATE_REPO
            self._repo.remotes[0].fetch()

        self._current_operation = CommitCrawlerState.GET_PREVIOUS_COMMITS
        self._commit_provider.cache_from_db()

        self._current_operation = CommitCrawlerState.EXTRACT_COMMITS
        all_hashes = self._repo.git.execute('git rev-list --all').splitlines()
        self._child_commit_graph = self.__extract_child_tree(all_hashes)

        self._current_operation = CommitCrawlerState.CALCULATE_PATHS
        self._latest_hashes = self.__get_latest_commits_of_branches(limit_tracked_branches_days_last_activity)
        current_paths_of_branches = self.__follow_files()

        return CrawlResult(self._child_commit_graph, current_paths_of_branches)

    def __get_latest_commits_of_branches(self, limit_tracked_branches_days_last_activity):
        minTimestamp = (time.time() - (limit_tracked_branches_days_last_activity * 24 * 3600)
                        if limit_tracked_branches_days_last_activity else 0)
        commit_hash_of_branch = {}
        for branch in self.__get_repo_branches():
            commit_hash = self._repo.git.execute(f'git rev-parse "{branch}"').strip()
            commit_metadata = self._commit_provider.get_cached_commit(commit_hash).metadata
            if commit_metadata.authored_timestamp >= minTimestamp:
                commit_hash_of_branch[commit_hash] = self.__clean_origin_branch_name(branch)
        return commit_hash_of_branch

    def __get_repo_branches(self):
        if len(self._repo.remotes) > 0:
            return [ref.name for ref in self._repo.remotes[0].refs]
        return [branch.name for branch in self._repo.branches]

    def __clean_origin_branch_name(self, branch_name):
        # We use the branches available from the origin remote repo, but want to present them
        # as "master" instead of "origin/master"
        return branch_name.split('/')[-1]

    def __extract_child_tree(self, all_hashes):
        self._commits_total = len(all_hashes)

        children = defaultdict(list)
        for i, hash in enumerate(all_hashes):
            commit = self._repo.commit(hash)
            # When merging there is only a commit in the target branch, not the others that got merged in
            for parent in commit.parents[:1]:
                children[parent.hexsha].append(self.__create_child_commit_entry(commit))

            self._commits_processed = i + 1

        initial_hash = all_hashes[-1]
        initial_commit = self._repo.commit(initial_hash)
        children['empty'].append(self.__create_child_commit_entry(initial_commit))
        return children

    def __create_child_commit_entry(self, commit):
        return self._commit_provider.get_commit(commit)

    def __follow_files(self):
        current_paths_of_branches = CurrentFilesInfoCollector(self._repo)
        self.__follow_file_renames_from_commit('empty', current_paths_of_branches, bidict())
        return current_paths_of_branches

    def __follow_file_renames_from_commit(self, commit_hash, files_info_collector: CurrentFilesInfoCollector,
                                          branch_file_paths: bidict):
        current_commit_hash = commit_hash
        while True:
            if current_commit_hash not in self._child_commit_graph:
                return
            number_child_commits = len(self._child_commit_graph[current_commit_hash])
            if number_child_commits == 0:
                return
            if number_child_commits == 1:
                current_commit_hash = self.__process_child_commmit(self._child_commit_graph[current_commit_hash][0],
                                                                   files_info_collector, branch_file_paths)
            else:
                for child_commit in self._child_commit_graph[current_commit_hash]:
                    sub_branch_file_paths = copy(branch_file_paths)
                    child_commit_hash = self.__process_child_commmit(child_commit, files_info_collector,
                                                                     sub_branch_file_paths)
                    self.__follow_file_renames_from_commit(child_commit_hash, files_info_collector,
                                                           sub_branch_file_paths)  # Only use recursion at commit graph branches
                return

    def __process_child_commmit(self, child_commit: Commit, files_info_collector: CurrentFilesInfoCollector,
                                branch_file_paths: bidict):
        for affected_file in child_commit.affected_files:
            change_type = affected_file.change_type
            if change_type == 'A':
                file_id = affected_file.file_id or self._get_unique_id()
            else:
                file_id = branch_file_paths.inverse[affected_file.old_path]

            affected_file.file_id = file_id
            branch_file_paths[file_id] = affected_file.new_path
            if change_type == 'D':
                del branch_file_paths[file_id]
        child_commit_hash = child_commit.hash
        if child_commit_hash in self._latest_hashes:
            branch_name = self._latest_hashes[child_commit_hash]
            files_info_collector.add_branch_info(branch_name, child_commit_hash, dict(branch_file_paths))
        return child_commit_hash

    def _get_unique_id(self):
        return str(uuid4())

    def __del__(self):
        if self._repo:
            self._repo.__del__()

class CrawlResult:
    def __init__(self, child_commit_tree: Dict[str, List[Commit]], current_paths_of_branches: CurrentFilesInfoCollector):
        self.child_commit_tree = child_commit_tree
        self.current_info_of_branches = current_paths_of_branches

    def write_to_db(self):
        db_engine = db.get_engine()

        db.create_tables()
        db.SqlCurrentFileInfo.__table__.drop(db_engine)
        db.create_tables()

        db_session = db.get_session()
        for commits in self.child_commit_tree.values():
            for commit in commits:
                commit.add_to_db(db_session)

        self.current_info_of_branches.add_to_db(db_session)

        db_session.commit()


if __name__ == '__main__':
    crawler = CommitCrawler(REPO_PATH, CommitProvider())
    crawler.crawl()
    print('Finished!')
