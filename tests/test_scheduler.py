import shutil
import tempfile
import unittest

from twisted.internet import defer

from scrapy.crawler import Crawler
from scrapy.core.scheduler import Scheduler
from scrapy.http import Request
from scrapy.pqueues import _scheduler_slot_read, _scheduler_slot_write
from scrapy.signals import request_reached_downloader, response_downloaded
from scrapy.spiders import Spider
from scrapy.utils.test import get_crawler
from tests.mockserver import MockServer


class MockCrawler(Crawler):
    def __init__(self, priority_queue_cls, jobdir):

        settings = dict(
                LOG_UNSERIALIZABLE_REQUESTS=False,
                SCHEDULER_DISK_QUEUE='scrapy.squeues.PickleLifoDiskQueue',
                SCHEDULER_MEMORY_QUEUE='scrapy.squeues.LifoMemoryQueue',
                SCHEDULER_PRIORITY_QUEUE=priority_queue_cls,
                JOBDIR=jobdir,
                DUPEFILTER_CLASS='scrapy.dupefilters.BaseDupeFilter'
                )
        super(MockCrawler, self).__init__(Spider, settings)


class SchedulerHandler:
    priority_queue_cls = None
    jobdir = None

    def create_scheduler(self):
        self.mock_crawler = MockCrawler(self.priority_queue_cls, self.jobdir)
        self.scheduler = Scheduler.from_crawler(self.mock_crawler)
        self.spider = Spider(name='spider')
        self.scheduler.open(self.spider)

    def close_scheduler(self):
        self.scheduler.close('finished')
        self.mock_crawler.stop()

    def setUp(self):
        self.create_scheduler()

    def tearDown(self):
        self.close_scheduler()


_PRIORITIES = [("http://foo.com/a", -2),
               ("http://foo.com/d", 1),
               ("http://foo.com/b", -1),
               ("http://foo.com/c", 0),
               ("http://foo.com/e", 2)]


_URLS = {"http://foo.com/a", "http://foo.com/b", "http://foo.com/c"}


class BaseSchedulerInMemoryTester(SchedulerHandler):
    def test_length(self):
        self.assertFalse(self.scheduler.has_pending_requests())
        self.assertEqual(len(self.scheduler), 0)

        for url in _URLS:
            self.scheduler.enqueue_request(Request(url))

        self.assertTrue(self.scheduler.has_pending_requests())
        self.assertEqual(len(self.scheduler), len(_URLS))

    def test_dequeue(self):
        for url in _URLS:
            self.scheduler.enqueue_request(Request(url))

        urls = set()
        while self.scheduler.has_pending_requests():
            urls.add(self.scheduler.next_request().url)

        self.assertEqual(urls, _URLS)

    def test_dequeue_priorities(self):
        for url, priority in _PRIORITIES:
            self.scheduler.enqueue_request(Request(url, priority=priority))

        priorities = list()
        while self.scheduler.has_pending_requests():
            priorities.append(self.scheduler.next_request().priority)

        self.assertEqual(priorities,
                         sorted([x[1] for x in _PRIORITIES], key=lambda x: -x))


class BaseSchedulerOnDiskTester(SchedulerHandler):

    def setUp(self):
        self.jobdir = tempfile.mkdtemp()
        self.create_scheduler()

    def tearDown(self):
        self.close_scheduler()

        shutil.rmtree(self.jobdir)
        self.jobdir = None

    def test_length(self):
        self.assertFalse(self.scheduler.has_pending_requests())
        self.assertEqual(len(self.scheduler), 0)

        for url in _URLS:
            self.scheduler.enqueue_request(Request(url))

        self.close_scheduler()
        self.create_scheduler()

        self.assertTrue(self.scheduler.has_pending_requests())
        self.assertEqual(len(self.scheduler), len(_URLS))

    def test_dequeue(self):
        for url in _URLS:
            self.scheduler.enqueue_request(Request(url))

        self.close_scheduler()
        self.create_scheduler()

        urls = set()
        while self.scheduler.has_pending_requests():
            urls.add(self.scheduler.next_request().url)

        self.assertEqual(urls, _URLS)

    def test_dequeue_priorities(self):
        for url, priority in _PRIORITIES:
            self.scheduler.enqueue_request(Request(url, priority=priority))

        self.close_scheduler()
        self.create_scheduler()

        priorities = list()
        while self.scheduler.has_pending_requests():
            priorities.append(self.scheduler.next_request().priority)

        self.assertEqual(priorities,
                         sorted([x[1] for x in _PRIORITIES], key=lambda x: -x))


class TestSchedulerInMemory(BaseSchedulerInMemoryTester, unittest.TestCase):
    priority_queue_cls = 'queuelib.PriorityQueue'


class TestSchedulerOnDisk(BaseSchedulerOnDiskTester, unittest.TestCase):
    priority_queue_cls = 'queuelib.PriorityQueue'


_SLOTS = [("http://foo.com/a", 'a'),
          ("http://foo.com/b", 'a'),
          ("http://foo.com/c", 'b'),
          ("http://foo.com/d", 'b'),
          ("http://foo.com/e", 'c'),
          ("http://foo.com/f", 'c')]


class TestMigration(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _migration(self, tmp_dir):
        prev_scheduler_handler = SchedulerHandler()
        prev_scheduler_handler.priority_queue_cls = 'queuelib.PriorityQueue'
        prev_scheduler_handler.jobdir = tmp_dir

        prev_scheduler_handler.create_scheduler()
        for url in _URLS:
            prev_scheduler_handler.scheduler.enqueue_request(Request(url))
        prev_scheduler_handler.close_scheduler()

        next_scheduler_handler = SchedulerHandler()
        next_scheduler_handler.priority_queue_cls = 'scrapy.pqueues.DownloaderAwarePriorityQueue'
        next_scheduler_handler.jobdir = tmp_dir

        next_scheduler_handler.create_scheduler()

    def test_migration(self):
        with self.assertRaises(ValueError):
            self._migration(self.tmpdir)


class TestSchedulerWithDownloaderAwareInMemory(BaseSchedulerInMemoryTester,
                                               unittest.TestCase):
    priority_queue_cls = 'scrapy.pqueues.DownloaderAwarePriorityQueue'

    def test_logic(self):
        for url, slot in _SLOTS:
            request = Request(url)
            _scheduler_slot_write(request, slot)
            self.scheduler.enqueue_request(request)

        slots = list()
        requests = list()
        while self.scheduler.has_pending_requests():
            request = self.scheduler.next_request()
            slots.append(_scheduler_slot_read(request))
            self.mock_crawler.signals.send_catch_log(
                    signal=request_reached_downloader,
                    request=request,
                    spider=self.spider
                    )
            requests.append(request)
        self.assertEqual(len(slots), len(_SLOTS))

        for request in requests:
            self.mock_crawler.signals.send_catch_log(
                    signal=response_downloaded,
                    request=request,
                    response=None,
                    spider=self.spider
                    )

        unique_slots = len(set(s for _, s in _SLOTS))
        for i in range(0, len(_SLOTS), unique_slots):
            part = slots[i:i + unique_slots]
            self.assertEqual(len(part), len(set(part)))


def _is_slots_unique(base_slots, result_slots):
    unique_slots = len(set(s for _, s in base_slots))
    for i in range(0, len(result_slots), unique_slots):
        part = result_slots[i:i + unique_slots]
        assert len(part) == len(set(part))


class TestSchedulerWithDownloaderAwareOnDisk(BaseSchedulerOnDiskTester,
                                             unittest.TestCase):
    priority_queue_cls = 'scrapy.pqueues.DownloaderAwarePriorityQueue'

    def test_logic(self):
        for url, slot in _SLOTS:
            request = Request(url)
            _scheduler_slot_write(request, slot)
            self.scheduler.enqueue_request(request)

        self.close_scheduler()
        self.create_scheduler()

        slots = list()
        requests = list()
        while self.scheduler.has_pending_requests():
            request = self.scheduler.next_request()
            slots.append(_scheduler_slot_read(request))
            self.mock_crawler.signals.send_catch_log(
                    signal=request_reached_downloader,
                    request=request,
                    spider=self.spider
                    )
            requests.append(request)

        self.assertEqual(self.scheduler.mqs._slots, {})
        self.assertEqual(len(slots), len(_SLOTS))

        for request in requests:
            self.mock_crawler.signals.send_catch_log(
                    signal=response_downloaded,
                    request=request,
                    response=None,
                    spider=self.spider
                    )

        _is_slots_unique(_SLOTS, slots)


class SlotCollectorSpider(Spider):

    def __init__(self, start_slots):
        self.start_slots = start_slots

    def start_requests(self):
        for url, slot in self.start_slots:
            request = Request(url)
            _scheduler_slot_write(request, slot)
            for d in self._force_crawl(request):
                yield d

    def parse(self, response):
        slots = getattr(self, 'slots', list())
        slots.append(_scheduler_slot_read(response.request))
        setattr(self, 'slots', slots)

    def _force_crawl(self, request):
        """
        we have to do this until problem in https://github.com/scrapy/scrapy/pull/3237
        is not solved. Otherwise priority queue has no chance to schedule these
        requests
        """
        try:
            self.crawler.engine.crawl(request, self)
        except AssertionError:
            yield request


@defer.inlineCallbacks
def test_integration_downloader_aware_priority_queue():
    with MockServer() as mockserver:

        url = mockserver.url("/status?n=200", is_secure=False)

        slots = [(url, 'a'),
                 (url, 'a'),
                 (url, 'b'),
                 (url, 'b'),
                 (url, 'c'),
                 (url, 'c')]

        crawler = get_crawler(
                SlotCollectorSpider,
                {'SCHEDULER_PRIORITY_QUEUE': 'scrapy.pqueues.DownloaderAwarePriorityQueue',
                 'DUPEFILTER_CLASS': 'scrapy.dupefilters.BaseDupeFilter'}
                )

        yield crawler.crawl(slots)
        spider = crawler.spider

        assert len(spider.slots) == len(slots)
        _is_slots_unique(slots, spider.slots)
