import hashlib
import json

from typing import Dict, List, Optional, Tuple
from .client import CacheAction
from .utils import streamed_errors

# New imports

from metaflow import namespace, Task
namespace(None)  # Always use global namespace by default

STDOUT = 'log_location_stdout'
STDERR = 'log_location_stderr'


class GetLogFile(CacheAction):
    """
    Gets logs for a task, returning a paginated subset of the loglines.

    Parameters
    ----------
    task : Dict
        Task dictionary, example:
        {
            "flow_id": "TestFlow",
            "run_number": 1234,
            "step_name": "regular_step",
            "task_id": 456,
            "attempt_id": 0
        }

    logtype : str
        Type of log to fetch, possible values "stdout" and "stderr"

    limit : int
        how many rows to return from the logs.

    page : int
        which one of the limited log row sets to return.

    reverse_order : bool
        Reverse the log row order.

    raw_log : bool
        Control whether to return a list of dictionaries, or the raw log string contents.

    invalidate_cache: Boolean
        Whether to invalidate the cache or not,
        use this to force a re-check and possible refetch of the log file.

    Returns
    --------
    Dict
        example:
        {
            "content": [
                {"row": 1, "line": "first log line},
                {"row": 2, "line": "second log line},
            ],
            "pages": 5
        }
    """

    @classmethod
    def format_request(cls, task: Dict, logtype: str = STDOUT,
                       limit: int = 0, page: int = 1,
                       reverse_order: bool = False, raw_log: bool = False, invalidate_cache=False
                       ):
        msg = {
            'task': task,
            'logtype': logtype,
            'limit': limit,
            'page': page,
            'reverse_order': reverse_order,
            'raw_log': raw_log
        }
        log_key = log_cache_id(task, logtype)
        result_key = log_result_id(task, logtype, limit, page, reverse_order, raw_log)
        stream_key = 'log:stream:%s' % lookup_id(task, logtype, limit, page, reverse_order, raw_log)

        return msg,\
            [log_key, result_key],\
            stream_key,\
            [stream_key, result_key],\
            invalidate_cache

    @classmethod
    def response(cls, keys_objs):
        '''
        Return the cached log content
        '''
        return [
            json.loads(val) for key, val in keys_objs.items()
            if key.startswith('log:result')][0]

    @classmethod
    def stream_response(cls, it):
        for msg in it:
            yield msg

    @classmethod
    def execute(cls,
                message=None,
                keys=None,
                existing_keys={},
                stream_output=None,
                invalidate_cache=False,
                **kwargs):

        results = {}
        # params
        task_dict = message['task']
        attempt = int(task_dict.get('attempt_id', 0))
        limit = message['limit']
        page = message['page']
        logtype = message['logtype']
        reverse = message['reverse_order']
        output_raw = message['raw_log']
        pathspec = pathspec_for_task(task_dict)

        # keys
        log_key = log_cache_id(task_dict, logtype)
        result_key = log_result_id(task_dict, logtype, limit, page, reverse, output_raw)

        previous_log_file = existing_keys.get(log_key, None)
        previous_log_size = json.loads(previous_log_file).get("log_size", None) if previous_log_file else None

        log_size_changed = False  # keep track if we loaded new content
        with streamed_errors(stream_output):
            task = Task(pathspec, attempt=attempt)
            # check if log has grown since last time.
            current_size = get_log_size(task, logtype)
            log_size_changed = previous_log_size is None or previous_log_size != current_size

            if log_size_changed:
                content = get_log_content(task, logtype)
                results[log_key] = json.dumps({"log_size": current_size, "content": content})
            else:
                results = {**existing_keys}

        if log_size_changed or result_key not in existing_keys:
            results[result_key] = json.dumps(
                paginated_result(
                    json.loads(results[log_key])["content"],
                    page,
                    limit,
                    reverse,
                    output_raw
                )
            )

        return results

# Utilities


def get_log_size(task: Task, logtype: str):
    return task.stderr_size if logtype == STDERR else task.stdout_size


def get_log_content(task: Task, logtype: str):
    # NOTE: this re-implements some of the client logic from _load_log(self, stream)
    # for backwards compatibility of different log types.
    # Necessary due to the client not exposing a stdout/stderr property that would
    # contain the optional timestamps.
    stream = 'stderr' if logtype == STDERR else 'stdout'
    log_location = task.metadata_dict.get('log_location_%s' % stream)
    if log_location:
        return [
            (None, line)
            for line in task._load_log_legacy(log_location, stream).split("\n")
        ]
    else:
        return [
            (_datetime_to_epoch(datetime), line)
            for datetime, line in task.loglines(stream)
        ]


def paginated_result(content: List[Tuple[Optional[int], str]], page: int = 1, limit: int = 0, reverse_order: bool = False, output_raw=False):
    if not output_raw:
        loglines, total_pages = format_loglines(content, page, limit, reverse_order)
    else:
        loglines = "\n".join(line for _, line in content)
        total_pages = 1

    return {
        "content": loglines,
        "pages": total_pages
    }


def format_loglines(content: List[Tuple[Optional[int], str]], page: int = 1, limit: int = 0, reverse: bool = False) -> Tuple[List, int]:
    "format, order and limit the log content. Return a list of log lines with row numbers"
    lines = [
        {"row": row, "timestamp": line[0], "line": line[1]}
        for row, line in enumerate(content)
    ]

    _offset = limit * (page - 1)
    pages = max(len(lines) // limit, 1) if limit else 1
    if pages < page:
        # guard against OOB page requests
        return [], pages

    if reverse:
        lines = lines[::-1]

    if limit:
        lines = lines[_offset:][:limit]

    return lines, pages


def log_cache_id(task: Dict, logtype: str):
    "construct a unique cache key for log file location"
    return "log:file:{pathspec}.{attempt_id}.{logtype}".format(
        pathspec=pathspec_for_task(task),
        attempt_id=task.get("attempt_id", 0),
        logtype=logtype
    )


def log_result_id(task: Dict, logtype: str, limit: int = 0, page: int = 1, reverse_order: bool = False, raw_log: bool = False):
    "construct a unique cache key for a paginated log response"
    return "log:result:%s" % lookup_id(task, logtype, limit, page, reverse_order, raw_log)


def lookup_id(task: Dict, logtype: str, limit: int = 0, page: int = 1, reverse_order: bool = False, raw_log: bool = False):
    "construct a unique id to be used with stream_key and result_key"
    _string = "{file}_{limit}_{page}_{reverse}_{raw}".format(
        file=log_cache_id(task, logtype),
        limit=limit,
        page=page,
        reverse=reverse_order,
        raw=raw_log
    )
    return hashlib.sha1(_string.encode('utf-8')).hexdigest()


def pathspec_for_task(task: Dict):
    "pathspec for a task"
    # Prefer run_id over run_number
    # Prefer task_name over task_id
    return "{flow_id}/{run_id}/{step_name}/{task_name}".format(
        flow_id=task['flow_id'],
        run_id=task.get('run_id') or task['run_number'],
        step_name=task['step_name'],
        task_name=task.get('task_name') or task['task_id']
    )


def _datetime_to_epoch(datetime) -> Optional[int]:
    """convert datetime safely into an epoch in milliseconds"""
    try:
        return int(datetime.timestamp() * 1000)
    except Exception:
        # consider timestamp to be none if handling failed
        return None
