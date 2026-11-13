import { useEffect, useState } from "react";
import { fetchBlob } from "../client";
import { mediaFileName, previewApiPath, previewKind } from "../media";
import { errorText } from "../types";

interface Props {
  projectId: string;
  path: string;
  title?: string;
}

export default function MediaPreview({ projectId, path, title }: Props) {
  const kind = previewKind(path);
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let objectUrl = "";
    setUrl("");
    setText("");
    setError("");
    fetchBlob(previewApiPath(projectId, path))
      .then(async (blob) => {
        if (!active) return;
        if (kind === "text") {
          setText(await blob.text());
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((err) => {
        if (active) setError(errorText(err));
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, path, kind]);

  const name = title || mediaFileName(path);
  if (error) {
    return <p className="text-sm text-wine-600">{error}</p>;
  }
  if (kind === "text") {
    return (
      <div className="space-y-1">
        <p className="text-xs text-ink-500">{name}</p>
        {text ? (
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded bg-paper p-3 text-xs text-ink-700">
            {text}
          </pre>
        ) : (
          <p className="text-sm text-ink-500">加载中…</p>
        )}
      </div>
    );
  }
  if (!url) {
    return <p className="text-sm text-ink-500">加载中…</p>;
  }
  if (kind === "video") {
    return (
      <div className="space-y-1">
        <video
          className="max-h-[480px] w-full rounded bg-black"
          controls
          preload="metadata"
          src={url}
        />
        <a
          className="text-xs text-sage-700"
          href={url}
          download={mediaFileName(path)}
        >
          下载视频
        </a>
      </div>
    );
  }
  if (kind === "audio") {
    return (
      <div className="space-y-1">
        <audio className="w-full" controls src={url} />
        <a
          className="text-xs text-sage-700"
          href={url}
          download={mediaFileName(path)}
        >
          下载音频
        </a>
      </div>
    );
  }
  if (kind === "image") {
    return (
      <img
        className="max-h-72 rounded border border-line"
        src={url}
        alt={name}
      />
    );
  }
  return (
    <a
      className="text-sm text-sage-700"
      href={url}
      download={mediaFileName(path)}
    >
      下载 {mediaFileName(path)}
    </a>
  );
}
