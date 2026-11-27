import { useEffect, useState } from "react";
import { fetchBlob } from "../client";
import { previewApiPath } from "../media";
import { previewStyle, previewTitle } from "../preview";
import { platformLabel } from "../types";

interface Props {
  platform: string;
  contentKind: string;
  title: string;
  excerpt: string;
  cover: string | null;
  projectId: string;
  accountName: string | null;
}

function useCoverUrl(projectId: string, path: string | null): string {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!path) {
      setUrl("");
      return;
    }
    let active = true;
    let objectUrl = "";
    fetchBlob(previewApiPath(projectId, path))
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (active) setUrl("");
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, path]);
  return url;
}

export default function PlatformPreview({
  platform,
  contentKind,
  title,
  excerpt,
  cover,
  projectId,
  accountName,
}: Props) {
  const style = previewStyle(platform);
  const url = useCoverUrl(projectId, cover);
  const heading = previewTitle(title, platformLabel(platform));
  const account = accountName || "未关联账号";

  return (
    <div className="space-y-2">
      <div
        className={`overflow-hidden rounded-2xl border shadow-sm ${style.frame}`}
      >
        <div
          className={`flex items-center justify-between gap-2 px-4 py-2 text-xs ${style.header}`}
        >
          <span className="font-medium">{platformLabel(platform)}</span>
          <span className="truncate opacity-80">{account}</span>
        </div>
        {style.layout === "video" ? (
          <div className="relative mx-auto aspect-[9/16] w-full max-w-56 overflow-hidden bg-ink-900">
            {url ? (
              <img
                className="h-full w-full object-cover"
                src={url}
                alt={`${platformLabel(platform)} 封面`}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-white/60">
                暂无封面
              </div>
            )}
            <div className="absolute inset-x-0 bottom-0 space-y-1 bg-gradient-to-t from-black/70 to-transparent p-3">
              <p className={`line-clamp-2 text-sm leading-snug ${style.title}`}>
                {heading}
              </p>
              <p className="line-clamp-2 text-xs text-white/70">
                {excerpt || "暂无正文"}
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-2 p-4">
            <p className={`text-base leading-snug ${style.title}`}>{heading}</p>
            {url && (
              <img
                className="aspect-video w-full rounded-lg object-cover"
                src={url}
                alt={`${platformLabel(platform)} 封面`}
              />
            )}
            <p className="line-clamp-4 whitespace-pre-wrap text-sm leading-relaxed text-ink-700">
              {excerpt || "暂无正文"}
            </p>
            <p className={`text-xs ${style.accent}`}>
              {contentKind === "video" ? "视频内容" : "图文内容"}
            </p>
          </div>
        )}
      </div>
      <p className="text-xs text-ink-500">
        预览按平台版式近似渲染，实际效果以平台客户端为准。
      </p>
    </div>
  );
}
