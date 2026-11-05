import { describe, expect, it } from "vitest";
import {
  formatBytes,
  formatDuration,
  mediaFileName,
  previewApiPath,
  previewKind,
} from "../media";

describe("previewKind", () => {
  it("maps supported media extensions", () => {
    expect(previewKind("media/run/video.mp4")).toBe("video");
    expect(previewKind("media/run/voiceover.MP3")).toBe("audio");
    expect(previewKind("media/run/cover.png")).toBe("image");
    expect(previewKind("media/run/photo.JPG")).toBe("image");
    expect(previewKind("media/run/subtitles.srt")).toBe("text");
    expect(previewKind("media/run/script.txt?x=1")).toBe("text");
  });

  it("returns other for unknown or missing extensions", () => {
    expect(previewKind("media/run/page.html")).toBe("other");
    expect(previewKind("media/run/noext")).toBe("other");
  });
});

describe("previewApiPath", () => {
  it("encodes both the project id and file path", () => {
    expect(previewApiPath("p 1", "media/run/a b.mp4")).toBe(
      "/projects/p%201/files/preview?path=media%2Frun%2Fa%20b.mp4",
    );
  });
});

describe("mediaFileName", () => {
  it("returns the last path segment", () => {
    expect(mediaFileName("media/run/video.mp4")).toBe("video.mp4");
    expect(mediaFileName("plain.mp3")).toBe("plain.mp3");
  });
});

describe("formatDuration", () => {
  it("formats seconds, minutes and hours", () => {
    expect(formatDuration(8)).toBe("00:08");
    expect(formatDuration(75.4)).toBe("01:15");
    expect(formatDuration(3725)).toBe("01:02:05");
    expect(formatDuration(Number.NaN)).toBe("00:00");
    expect(formatDuration(-2)).toBe("00:00");
  });
});

describe("formatBytes", () => {
  it("formats byte sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
    expect(formatBytes(0)).toBe("0 B");
  });
});
