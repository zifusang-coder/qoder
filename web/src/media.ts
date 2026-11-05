export type PreviewKind = "video" | "audio" | "image" | "text" | "other";

const EXTENSION_KINDS: Record<string, PreviewKind> = {
  mp4: "video",
  mp3: "audio",
  png: "image",
  jpg: "image",
  jpeg: "image",
  webp: "image",
  srt: "text",
  txt: "text",
};

export function previewKind(path: string): PreviewKind {
  const clean = path.split("?")[0].split("#")[0].toLowerCase();
  const dot = clean.lastIndexOf(".");
  if (dot < 0) return "other";
  return EXTENSION_KINDS[clean.slice(dot + 1)] ?? "other";
}

export function previewApiPath(projectId: string, path: string): string {
  return `/projects/${encodeURIComponent(projectId)}/files/preview?path=${encodeURIComponent(path)}`;
}

export function mediaFileName(path: string): string {
  const clean = path.split("?")[0];
  const parts = clean.split("/");
  return parts[parts.length - 1] || path;
}

export function formatDuration(seconds: number): string {
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0
    ? `${pad(hours)}:${pad(minutes)}:${pad(secs)}`
    : `${pad(minutes)}:${pad(secs)}`;
}

export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
