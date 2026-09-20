import memento.utils as utils
import cv2
import numpy as np
import pygame
import datetime
from memento.timeline.apps import Apps


class TimeBar:
    def __init__(self, frame_getter):
        self.frame_getter = frame_getter
        self.nb_frames = self.frame_getter.nb_frames
        self.metadata_cache = self.frame_getter.metadata_cache
        self.show_bar = True
        self.bar_border_trigger = 10

        # Actual graphical window size
        ws = self.frame_getter.window_size
        self.h = ws[1] // 40
        self.x = 20
        self.y = ws[1] - self.h - ws[1] // 10
        self.w = ws[0] - self.x * 2

        self.min_tws = self.bar_border_trigger * 4

        # Time window size
        self.tws = min(self.nb_frames, int(10 * utils.FPS * utils.SECONDS_PER_REC))
        self.frame_offset = 0  # from the right
        self.compute_time_window()  # To be called when frame_offset changes

        self.current_frame_i = self.tw_end - 1
        self.preview_frame_i = 0
        self.preview_surf = None
        self.apps = Apps(self.frame_getter)
        self.today = datetime.datetime.now().strftime("%Y-%m-%d")

    def zoom(self, dir):
        self.tws = min(
            min(self.nb_frames, utils.MAX_TWS),
            max(self.min_tws, int(self.tws * (1 + dir * 0.1))),
        )

        self.compute_time_window()

    def compute_time_window(self):
        self.tw_end = int(self.nb_frames - self.frame_offset)
        self.tw_start = max(0, int(self.tw_end - self.tws))

    def get_friendly_date(self, date):
        day = date.split(" ")[0]
        hr = date.split(" ")[1]
        if day == self.today:
            return "Today" + " " + hr
        elif day == (datetime.datetime.now() - datetime.timedelta(days=1)).strftime(
            "%Y-%m-%d"
        ):
            return "Yesterday" + " " + hr
        elif day == (datetime.datetime.now() - datetime.timedelta(days=2)).strftime(
            "%Y-%m-%d"
        ):
            return "2 days ago" + " " + hr
        else:
            return date

    # TODO adjust time bar so that the cursor is visible when jumping that way
    def set_current_frame_i(self, frame_i):
        # nb_moves = frame_i - self.current_frame_i
        # dir = 1 if nb_moves > 0 else -1
        # print(self.current_frame_i, frame_i, nb_moves)
        # for _ in range(nb_moves):
        #     self.move_cursor(dir)
        # # self.move_cursor(nb_moves)
        self.current_frame_i = frame_i

    def move_cursor(self, delta):
        self.current_frame_i = max(
            self.tw_start, min(self.tw_end - 1, self.current_frame_i + delta)
        )

        if self.current_frame_i < self.tw_start + self.bar_border_trigger:
            self.frame_offset = min(self.nb_frames - self.tws, self.frame_offset + 1)
            self.compute_time_window()

        if self.current_frame_i > self.tw_end - self.bar_border_trigger:
            self.frame_offset = max(0, self.frame_offset - 1)
            self.compute_time_window()

    def get_frame_i(self, mouse_pos):
        return int((mouse_pos[0] - self.x) / self.w * self.tws) + self.tw_start

    def hover(self, mouse_pos):
        return utils.in_rect((self.x, self.y, self.w, self.h), mouse_pos)

    def draw_cursor(self, screen):
        cursor_x = self.x + ((self.current_frame_i - self.tw_start) / self.tws) * self.w
        pygame.draw.line(
            screen,
            (255, 255, 255),
            (cursor_x, self.y - self.h),
            (cursor_x, self.y + self.h * 2),
            5,
        )

        self.draw_time(screen, (cursor_x, self.y))

    def _frame_app(self, i):
        # Gap frames (dropped by the capture policy) and missing metadata
        # become the explicit gap sentinel; None means past the live edge.
        entry = self.metadata_cache.get_frame_metadata_or_none(i)
        if entry is None:
            return None
        if entry.get("decision") == "drop":
            return utils.GAP_APP
        return entry.get("window_title", utils.GAP_APP)

    def _build_segments(self):
        # Walk frames backwards from the live edge to the window start
        i = self.tw_end
        while i > self.tw_start and self._frame_app(i) is None:
            i -= 1
        if i <= self.tw_start:
            return []

        segments = []
        last_app = self._frame_app(i)
        segments.append({"app": last_app, "start": i, "end": i})
        for j in range(i, self.tw_start - 1, -1):
            app = self._frame_app(j)
            if app is None:
                app = last_app
            segments[-1]["start"] = j  # current segment
            if app != last_app:
                segments.append({"app": app, "start": j, "end": j})
            last_app = app
        return segments

    def _draw_gap_hatch(self, screen, seg_x, seg_y, seg_w, seg_h):
        # Diagonal hatch marks gap intervals as non-content areas
        step = 12
        for offset in range(-seg_h, int(seg_w) + seg_h, step):
            pygame.draw.line(
                screen,
                (120, 120, 120),
                (seg_x + offset, seg_y + seg_h),
                (seg_x + offset + seg_h, seg_y),
                1,
            )

    def draw_bar(self, screen, mouse_pos):
        segments = self._build_segments()

        for segment in segments:
            app = segment["app"]
            start = segment["start"] - self.tw_start
            end = segment["end"] - self.tw_start
            middle = (start + end) / 2
            seg_x = self.x + (start / self.tws) * self.w
            seg_w = max(1, (end - start) / self.tws * self.w)

            pygame.draw.rect(
                screen,
                self.apps.get_color(app),
                (seg_x, self.y, seg_w, self.h),
                border_radius=self.h // 4,
            )

            if app == utils.GAP_APP:
                self._draw_gap_hatch(
                    screen, seg_x, self.y, seg_w, self.h
                )

        for segment in segments:
            app = segment["app"]
            start = segment["start"] - self.tw_start
            end = segment["end"] - self.tw_start
            middle = (start + end) / 2
            seg_x = self.x + (start / self.tws) * self.w
            seg_w = max(1, (end - start) / self.tws * self.w)

            if self.apps.get_icon(app) is not None:
                if start <= self.current_frame_i <= end or utils.in_rect(
                    (seg_x, self.y, seg_w, self.h), mouse_pos
                ):
                    screen.blit(
                        self.apps.get_icon(app, small=False),
                        (
                            self.x + (middle / self.tws) * self.w - self.apps.ig.size,
                            self.y - self.apps.ig.size // 2,
                        ),
                    )

                else:
                    screen.blit(
                        self.apps.get_icon(app, small=True),
                        (
                            self.x
                            + (middle / self.tws) * self.w
                            - self.apps.ig.size // 2,
                            self.y,
                        ),
                    )

    # TODO cache previews ?
    def draw_preview(self, screen, mouse_pos):
        if not self.hover(mouse_pos):
            return
        frame_i = self.get_frame_i(mouse_pos)
        # Dropped intervals are never previewable
        if self.frame_getter.is_gap(frame_i):
            self.preview_surf = None
            return
        if frame_i != self.preview_frame_i or self.preview_surf is None:
            self.preview_frame_i = frame_i
            frame = self.frame_getter.get_frame(frame_i)
            frame = cv2.resize(frame, (0, 0), fx=0.2, fy=0.2)
            self.preview_surf = pygame.surfarray.make_surface(frame)
        screen.blit(
            self.preview_surf,
            [mouse_pos[0], self.y]
            - np.array(
                [self.preview_surf.get_size()[0] // 2, self.preview_surf.get_size()[1]]
            ),
        )

    def draw_time(self, screen, pos):
        if not self.hover(pos):
            return
        frame_i = self.get_frame_i(pos)
        if self.frame_getter.is_gap(frame_i):
            # Gaps show the rule hit reason instead of a timestamp/title
            reason = self.frame_getter.gap_reason(frame_i) or "policy"
            label = "Not recorded (" + str(reason) + ")"
        else:
            entry = self.metadata_cache.get_frame_metadata_or_none(frame_i)
            time = (entry or {}).get("time", '""').strip('"')
            label = self.get_friendly_date(time) if time else ""
        font = pygame.font.SysFont("Arial", 20)
        text = font.render(label, True, (0, 0, 0))
        text_size = text.get_size()
        border = 10
        pygame.draw.rect(
            screen,
            (255, 255, 255),
            (
                pos[0] - text_size[0] // 2 - border,
                self.y + text_size[1] + self.h - border,
                text_size[0] + border * 2,
                text_size[1] + border * 2,
            ),
            border_radius=self.h // 2,
        )

        screen.blit(text, [pos[0] - text_size[0] // 2, self.y + text_size[1] + self.h])
        pygame.draw.line(
            screen,
            (255, 255, 255),
            (pos[0], self.y),
            (pos[0], self.y + self.h),
            5,
        )

    def show(self):
        self.show_bar = True

    def hide(self):
        self.show_bar = False

    def draw(self, screen, mouse_pos):
        if self.show_bar:
            self.draw_preview(screen, mouse_pos)
            self.draw_bar(screen, mouse_pos)
            self.draw_cursor(screen)
            self.draw_time(screen, mouse_pos)
