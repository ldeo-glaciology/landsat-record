"""Progress bars that suit where the output goes, and a thread pool that reports through them."""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

PLAIN_INTERVAL = 60  # seconds between progress lines when output goes to a file


def in_notebook():
    try:
        from IPython import get_ipython
    except ImportError:
        return False
    shell = get_ipython()
    return shell is not None and "IPKernelApp" in shell.config


class PlainProgress:
    """Stands in for tqdm when output goes to a file, as with nohup: a line every PLAIN_INTERVAL seconds and one at
    the end, so the log reads well with tail -f."""

    def __init__(self, total, desc, unit):
        self.total, self.desc, self.unit = total, desc, unit
        self.n = 0
        self.postfix = ""
        self.start = self.last = time.monotonic()

    def update(self, n=1):
        self.n += n
        if time.monotonic() - self.last >= PLAIN_INTERVAL:
            self.line()

    def set_postfix_str(self, text, refresh=True):
        self.postfix = text

    def line(self):
        self.last = time.monotonic()
        elapsed = self.last - self.start
        text = f"{self.desc}: {self.n}/{self.total} {self.unit}, {tqdm.format_interval(elapsed)} elapsed"
        if 0 < self.n < self.total:
            text += f", about {tqdm.format_interval(elapsed / self.n * (self.total - self.n))} left"
        if self.postfix:
            text += f", {self.postfix}"
        print(text, flush=True)

    def write(self, text):
        print(text, flush=True)

    def close(self):
        self.line()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def progress_bar(total, desc, unit):
    """A tqdm bar in a terminal or notebook, otherwise a line a minute."""
    if in_notebook():
        from tqdm.auto import tqdm as notebook_tqdm

        return notebook_tqdm(total=total, desc=desc, unit=f" {unit}")
    if sys.stderr.isatty():
        return tqdm(total=total, desc=desc, unit=f" {unit}", dynamic_ncols=True)
    return PlainProgress(total, desc, unit)


def say(text):
    """Print without breaking a progress bar that is showing."""
    tqdm.write(text)


def pool_map(fn, items, workers, bar, on_result=None):
    """fn over items in a thread pool. Returns the results in the order of items.

    The bar moves as each item finishes, and on_result(item, result) is called then. Ctrl-C cancels the items that
    haven't started and waits for the ones in progress, so no file is left half written.
    """
    items = list(items)
    results = [None] * len(items)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for future in as_completed(futures):
            i = futures[future]
            results[i] = future.result()
            bar.update(1)
            if on_result:
                on_result(items[i], results[i])
    except BaseException as error:
        pool.shutdown(wait=False, cancel_futures=True)
        if isinstance(error, KeyboardInterrupt):
            bar.write("Stopping: waiting for the files in progress to finish. Re-run the same command to continue.")
        raise
    finally:
        pool.shutdown(wait=True)
    return results
