import av
import bisect
from memento.utils import FPS, SECONDS_PER_REC, FRAME_CACHE_SIZE, CACHE_PATH
import time
import os
import json


class Reader:
    def __init__(self, filename, offset=0, gap_counter=None):
        # gap_counter(frame_id) returns the number of dropped (gap) frames
        # with an id strictly smaller than frame_id inside this segment, so a
        # logical frame id can be translated to the actual decoded position.
        self.frames = []
        self.offset = offset
        self.gap_counter = gap_counter

        if not os.path.isfile(filename) or os.path.getsize(filename) == 0:
            # Segment where every frame was dropped
            return
        try:
            container = av.open(filename)
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                self.frames.append(frame)
            container.close()
        except Exception:
            # Empty/corrupt segment: treat it as having no frames
            self.frames = []

    def get_frame(self, frame_i):
        position = frame_i - self.offset
        if self.gap_counter is not None:
            position -= self.gap_counter(frame_i)
        if 0 <= position < len(self.frames):
            return self.frames[position].to_ndarray(format="bgr24")
        else:
            return None


class ReadersCache:
    def __init__(self, metadata_cache=None):
        self.readers = {}
        self.readers_ids = []  # in order to know the oldest reader
        self.cache_size = FRAME_CACHE_SIZE
        self.metadata_cache = metadata_cache

    def select_video_id(self, frame_id):
        return int(frame_id // (FPS * SECONDS_PER_REC))

    def _make_gap_counter(self, video_id):
        def count_gaps(frame_id):
            if self.metadata_cache is None:
                return 0
            gaps = self.metadata_cache.gap_frames_for_segment(video_id)
            return bisect.bisect_left(gaps, frame_id)

        return count_gaps

    def get_reader(self, frame_id):
        video_id = self.select_video_id(frame_id)
        offset = int(video_id * (FPS * SECONDS_PER_REC))
        if video_id not in self.readers:  # Caching reader
            start = time.time()
            self.readers[video_id] = Reader(
                os.path.join(CACHE_PATH, str(video_id) + ".mp4"),
                offset=offset,
                gap_counter=self._make_gap_counter(video_id),
            )
            self.readers_ids.append(video_id)
            # print(
            #     "Caching reader",
            #     video_id,
            #     "at",
            #     offset,
            #     "offset frames took",
            #     time.time() - start,
            #     "seconds",
            # )
            if len(self.readers) > self.cache_size:
                dumped_id = self.readers_ids[0]
                self.readers_ids = self.readers_ids[1:]
                # print("Dumping reader with id", dumped_id, "from cache")
                del self.readers[dumped_id]
        return self.readers[video_id]

    # Shorthand
    def get_frame(self, frame_id):
        return self.get_reader(frame_id).get_frame(frame_id)


class Metadata:
    def __init__(self, file_path):
        self.file_path = file_path
        if not os.path.exists(self.file_path):
            self.metadata = {}
        else:
            self.metadata = json.load(open(self.file_path))
        self._gap_frames = None

    def get_frame(self, frame_id):
        return self.metadata[str(frame_id)]

    def get_frame_or_none(self, frame_id):
        return self.metadata.get(str(frame_id))

    def write(self, frame_id, data):
        self.metadata[str(frame_id)] = data
        self._gap_frames = None
        json.dump(self.metadata, open(self.file_path, "w"))

    def gap_frames(self):
        # Sorted ids of frames dropped by the capture policy in this segment
        if self._gap_frames is None:
            self._gap_frames = sorted(
                int(frame_id)
                for frame_id, entry in self.metadata.items()
                if isinstance(entry, dict)
                and entry.get("decision") == "drop"
            )
        return self._gap_frames


class MetadataCache:
    def __init__(self):
        self.cache = {}
        self.cache_size = FRAME_CACHE_SIZE
        self.cache_ids = []

    def select_metadata_id(self, frame_id):
        return int(int(frame_id) // (FPS * SECONDS_PER_REC))

    def get_metadata(self, frame_id):
        metadata_id = self.select_metadata_id(frame_id)
        if metadata_id not in self.cache:
            start = time.time()
            self.cache[metadata_id] = Metadata(
                os.path.join(CACHE_PATH, str(metadata_id) + ".json")
            )
            # print(
            #     "Caching metadata",
            #     metadata_id,
            #     "took",
            #     time.time() - start,
            #     "seconds",
            # )
            self.cache_ids.append(metadata_id)
            if len(self.cache) > self.cache_size:
                dumped_id = self.cache_ids[0]
                self.cache_ids = self.cache_ids[1:]
                # print("Dumping metadata with id", dumped_id, "from cache")
                del self.cache[dumped_id]
        return self.cache[metadata_id]

    def get_frame_metadata(self, frame_id):
        metadata = self.get_metadata(frame_id)
        return metadata.get_frame(frame_id)

    def get_frame_metadata_or_none(self, frame_id):
        metadata = self.get_metadata(frame_id)
        return metadata.get_frame_or_none(frame_id)

    def gap_frames_for_segment(self, metadata_id):
        return self.get_metadata(metadata_id).gap_frames()

    def is_gap(self, frame_id):
        entry = self.get_frame_metadata_or_none(frame_id)
        return isinstance(entry, dict) and entry.get("decision") == "drop"

    def gap_reason(self, frame_id):
        entry = self.get_frame_metadata_or_none(frame_id)
        if isinstance(entry, dict) and entry.get("decision") == "drop":
            return entry.get("rule_id")
        return None

    def write(self, frame_id, data):
        metadata = self.get_metadata(frame_id)
        metadata.write(frame_id, data)
