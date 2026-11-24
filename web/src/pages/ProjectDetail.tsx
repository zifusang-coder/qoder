import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../auth-context";
import { api } from "../client";
import MediaPreview from "../components/MediaPreview";
import { useLiveSnapshot } from "../live";
import {
  formatDuration,
  isMediaJobActive,
  mediaFileName,
  mediaJobStatusLabel,
} from "../media";
import {
  FALLBACK_PLATFORMS,
  accountsForPlatform,
  capabilityFor,
  specFor,
  usePlatforms,
} from "../platforms";
import {
  PLATFORMS,
  contentKindLabel,
  contentStatusLabel,
  errorText,
  jobStatusLabel,
  platformLabel,
  projectStatusLabel,
  type Account,
  type Content,
  type Job,
  type MediaJob,
  type MediaList,
  type Project,
  type RepurposeResult,
  type VersionList,
} from "../types";

type Notice = { kind: "ok" | "error"; text: string };

const VOICES: [string, string][] = [
  ["zh-CN-XiaoxiaoNeural", "晓晓 · 温暖女声"],
  ["zh-CN-XiaoyiNeural", "晓伊 · 活泼女声"],
  ["zh-CN-YunxiNeural", "云希 · 清朗男声"],
  ["zh-CN-YunyangNeural", "云扬 · 沉稳男声"],
  ["zh-CN-YunjianNeural", "云健 · 浑厚男声"],
  ["zh-HK-HiuMaanNeural", "曉曼 · 粤语女声"],
  ["zh-TW-HsiaoChenNeural", "曉臻 · 台湾女声"],
];

type MediaMode = "short-video" | "tts-voiceover";


export default function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const projectId = id ?? "";
  const qc = useQueryClient();
  const { canWrite } = useAuth();

  const [platform, setPlatform] = useState("wechat");
  const [kind, setKind] = useState("article");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [openVersions, setOpenVersions] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [targetPlatform, setTargetPlatform] = useState("xiaohongshu");
  const [jobNotice, setJobNotice] = useState<Notice | null>(null);
  const [repurposeNotice, setRepurposeNotice] = useState<Notice | null>(null);
  const [accountSelections, setAccountSelections] = useState<
    Record<string, string>
  >({});
  const [mediaMode, setMediaMode] = useState<MediaMode>("short-video");
  const [mediaTopic, setMediaTopic] = useState("");
  const [mediaText, setMediaText] = useState("");
  const [mediaTitle, setMediaTitle] = useState("");
  const [mediaPlatform, setMediaPlatform] = useState("douyin");
  const [mediaDuration, setMediaDuration] = useState(30);
  const [mediaVoice, setMediaVoice] = useState(true);
  const [mediaVoiceName, setMediaVoiceName] = useState(VOICES[0][0]);
  const [mediaTheme, setMediaTheme] = useState<"dark" | "light" | "warm">(
    "dark",
  );
  const [mediaRate, setMediaRate] = useState("+0%");
  const [mediaNotice, setMediaNotice] = useState<Notice | null>(null);
  const [activeMediaJobId, setActiveMediaJobId] = useState("");
  const [openMedia, setOpenMedia] = useState("");

  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api<Project>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
  });

  const contents = useQuery({
    queryKey: ["contents", projectId],
    queryFn: () => api<Content[]>(`/projects/${projectId}/contents`),
    enabled: Boolean(projectId),
  });

  const media = useQuery({
    queryKey: ["media", projectId],
    queryFn: () => api<MediaList>(`/projects/${projectId}/media`),
    enabled: Boolean(projectId),
  });

  const mediaJob = useQuery({
    queryKey: ["mediaJob", projectId, activeMediaJobId],
    queryFn: () =>
      api<MediaJob>(`/projects/${projectId}/media/jobs/${activeMediaJobId}`),
    enabled: Boolean(projectId && activeMediaJobId),
  });
  useLiveSnapshot<MediaJob>(
    ["mediaJob", projectId, activeMediaJobId],
    activeMediaJobId
      ? `/projects/${projectId}/media/jobs/${activeMediaJobId}/stream`
      : null,
  );

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Account[]>("/accounts"),
  });
  const platforms = usePlatforms();
  const platformSpecs = platforms.data ?? FALLBACK_PLATFORMS;
  const contentSpec = specFor(platform, platformSpecs);
  const contentKinds = Object.keys(
    contentSpec?.content_capabilities ?? { article: "pending" },
  );

  function selectContentPlatform(next: string) {
    setPlatform(next);
    const spec = specFor(next, platformSpecs);
    const nextKind =
      Object.keys(spec?.content_capabilities ?? {})[0] ?? "article";
    setKind(nextKind);
  }

  const versions = useQuery({
    queryKey: ["versions", openVersions],
    queryFn: () =>
      api<VersionList>(`/contents/${openVersions}/versions`),
    enabled: Boolean(openVersions),
  });

  const createContent = useMutation({
    mutationFn: (payload: {
      platform: string;
      kind: string;
      title: string;
      body: string;
    }) =>
      api<Content>(`/projects/${projectId}/contents`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (_content, payload) => {
      qc.invalidateQueries({ queryKey: ["contents", projectId] });
      setTitle((prev) => (prev.trim() === payload.title ? "" : prev));
      setBody((prev) => (prev === payload.body ? "" : prev));
    },
  });

  const createJob = useMutation({
    mutationFn: ({
      content,
      accountId,
    }: {
      content: Content;
      accountId: string;
    }) =>
      api<Job>("/publish/jobs", {
        method: "POST",
        body: JSON.stringify({
          content_id: content.id,
          platform: content.platform,
          ...(accountId ? { account_id: accountId } : {}),
        }),
      }),
    onSuccess: (job, { content }) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      // The endpoint is idempotent: it may replay an existing job, so report
      // the content it belongs to rather than claiming a new job was created.
      setJobNotice({
        kind: "ok",
        text: `已为「${content.title || "未命名内容"}」创建发布任务（${jobStatusLabel(job.status)}）`,
      });
    },
    onError: (err) => setJobNotice({ kind: "error", text: errorText(err) }),
  });

  const repurpose = useMutation({
    mutationFn: (payload: { source_content_id: string; platform: string }) =>
      api<RepurposeResult>(`/projects/${projectId}/repurpose`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["contents", projectId] });
      qc.invalidateQueries({ queryKey: ["versions", result.content_id] });
      setRepurposeNotice({
        kind: "ok",
        text: `已生成${platformLabel(result.platform)}版本 v${result.version}`,
      });
    },
    onError: (err) =>
      setRepurposeNotice({ kind: "error", text: errorText(err) }),
  });

  const createMedia = useMutation({
    mutationFn: ({
      skill,
      body,
    }: {
      skill: MediaMode;
      body: Record<string, unknown>;
    }) =>
      api<MediaJob>(`/projects/${projectId}/media/${skill}`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (job) => {
      setActiveMediaJobId(job.id);
      setMediaNotice({
        kind: "ok",
        text: "任务已提交，正在后台生成…",
      });
    },
    onError: (err) => setMediaNotice({ kind: "error", text: errorText(err) }),
  });

  const retryMedia = useMutation({
    mutationFn: (jobId: string) =>
      api<MediaJob>(`/projects/${projectId}/media/jobs/${jobId}/retry`, {
        method: "POST",
      }),
    onSuccess: (job) => {
      setActiveMediaJobId(job.id);
      setMediaNotice({ kind: "ok", text: "已重新提交生成任务" });
      qc.invalidateQueries({
        queryKey: ["mediaJob", projectId, job.id],
      });
    },
    onError: (err) => setMediaNotice({ kind: "error", text: errorText(err) }),
  });

  const activeMediaJob = mediaJob.data;
  const mediaJobBusy =
    createMedia.isPending || isMediaJobActive(activeMediaJob?.status);

  useEffect(() => {
    const job = mediaJob.data;
    if (!job || job.status !== "succeeded") return;
    qc.invalidateQueries({ queryKey: ["media", projectId] });
    qc.invalidateQueries({ queryKey: ["contents", projectId] });
    setMediaNotice({
      kind: "ok",
      text: `已生成${job.skill_id === "short-video" ? "短视频" : "配音"}，可在下方预览`,
    });
    setMediaTopic("");
    setMediaText("");
    setMediaTitle("");
  }, [mediaJob.data, projectId, qc]);

  function submitContent(event: FormEvent) {
    event.preventDefault();
    createContent.mutate({
      platform,
      kind,
      title: title.trim(),
      body,
    });
  }

  function submitRepurpose(event: FormEvent) {
    event.preventDefault();
    const effectiveSource = sourceId || contents.data?.[0]?.id || "";
    if (!effectiveSource) {
      setRepurposeNotice({ kind: "error", text: "请先创建内容" });
      return;
    }
    setRepurposeNotice(null);
    repurpose.mutate({
      source_content_id: effectiveSource,
      platform: targetPlatform,
    });
  }

  function submitMedia(event: FormEvent) {
    event.preventDefault();
    if (mediaMode === "short-video" && !mediaTopic.trim()) {
      setMediaNotice({ kind: "error", text: "请填写视频主题" });
      return;
    }
    if (mediaMode === "tts-voiceover" && !mediaText.trim()) {
      setMediaNotice({ kind: "error", text: "请填写口播稿" });
      return;
    }
    setMediaNotice(null);
    const common = {
      title: mediaTitle.trim(),
      platform: mediaPlatform,
      voice_name: mediaVoiceName,
    };
    createMedia.mutate({
      skill: mediaMode,
      body:
        mediaMode === "short-video"
          ? {
              ...common,
              topic: mediaTopic.trim(),
              duration_seconds: mediaDuration,
              voice: mediaVoice,
              theme: mediaTheme,
            }
          : { ...common, text: mediaText, rate: mediaRate },
    });
  }

  const effectiveSource = sourceId || contents.data?.[0]?.id || "";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link className="text-sm text-sage-700" to="/projects">
            ← 返回项目
          </Link>
          <h1 className="mt-2 text-xl font-semibold">
            {project.data?.title ?? "项目详情"}
          </h1>
          {project.data?.topic && (
            <p className="mt-1 text-sm text-ink-500">{project.data.topic}</p>
          )}
        </div>
        <span className="text-xs text-ink-500">
          {project.data ? `状态：${projectStatusLabel(project.data.status)}` : ""}
        </span>
      </div>

      {project.isError && (
        <p className="text-sm text-wine-600">{errorText(project.error)}</p>
      )}

      <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
        <div className="space-y-6">
          {canWrite && (
          <form
            className="space-y-3 card p-5"
            onSubmit={submitContent}
          >
            <h2 className="font-medium">新建内容</h2>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>平台</span>
              <select
                className="input text-ink-900"
                value={platform}
                onChange={(e) => selectContentPlatform(e.target.value)}
              >
                {platformSpecs.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>内容类型</span>
              <select
                className="input text-ink-900"
                value={kind}
                onChange={(e) => setKind(e.target.value)}
              >
                {contentKinds.map((value) => (
                  <option key={value} value={value}>
                    {contentKindLabel(value)}
                  </option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>标题</span>
              <input
                className="input text-ink-900"
                maxLength={300}
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="内容标题"
              />
            </label>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>正文</span>
              <textarea
                className="input text-ink-900"
                rows={10}
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder="粘贴或撰写正文"
              />
            </label>
            {createContent.isError && (
              <p className="text-sm text-wine-600">
                {errorText(createContent.error)}
              </p>
            )}
            <button
              className="btn-primary w-full"
              disabled={createContent.isPending}
            >
              {createContent.isPending ? "创建中…" : "创建内容"}
            </button>
          </form>
          )}

          {canWrite && (
          <form
            className="space-y-3 card p-5"
            onSubmit={submitRepurpose}
          >
            <h2 className="font-medium">一稿多改</h2>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>源内容</span>
              <select
                className="input text-ink-900"
                value={effectiveSource}
                onChange={(e) => setSourceId(e.target.value)}
                disabled={!contents.data || contents.data.length === 0}
              >
                {(contents.data ?? []).map((content) => (
                  <option key={content.id} value={content.id}>
                    {platformLabel(content.platform)} ·{" "}
                    {content.title || "未命名"}（v{content.current_version}）
                  </option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>目标平台</span>
              <select
                className="input text-ink-900"
                value={targetPlatform}
                onChange={(e) => setTargetPlatform(e.target.value)}
              >
                {PLATFORMS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <p className="text-xs text-ink-500">
              将调用 content-repurposing 技能生成目标平台版本。
            </p>
            <button
              className="btn-primary w-full"
              disabled={
                repurpose.isPending ||
                !contents.data ||
                contents.data.length === 0
              }
            >
              {repurpose.isPending ? "改写中…" : "生成新版本"}
            </button>
            {repurposeNotice && (
              <p
                className={
                  repurposeNotice.kind === "ok"
                    ? "text-sm text-sage-600"
                    : "text-sm text-wine-600"
                }
              >
                {repurposeNotice.text}
              </p>
            )}
          </form>
          )}

          {canWrite && (
          <form
            className="space-y-3 card p-5"
            onSubmit={submitMedia}
          >
            <h2 className="font-medium">AI 媒体生成</h2>
            <div className="grid grid-cols-2 gap-2">
              {(
                [
                  ["short-video", "短视频"],
                  ["tts-voiceover", "口播配音"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={
                    mediaMode === value
                      ? "btn-primary px-3"
                      : "rounded border border-line px-3 py-2 text-sm text-ink-700"
                  }
                  onClick={() => setMediaMode(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            {mediaMode === "short-video" ? (
              <>
                <label className="block space-y-1 text-sm text-ink-500">
                  <span>视频主题</span>
                  <input
                    className="input text-ink-900"
                    maxLength={500}
                    required
                    value={mediaTopic}
                    onChange={(e) => setMediaTopic(e.target.value)}
                    placeholder="例如：3 个方法让周报自动化"
                  />
                </label>
                <div className="grid grid-cols-2 gap-2">
                  <label className="block space-y-1 text-sm text-ink-500">
                    <span>时长（秒）</span>
                    <input
                      className="input text-ink-900"
                      type="number"
                      min={8}
                      max={180}
                      value={mediaDuration}
                      onChange={(e) =>
                        setMediaDuration(Number(e.target.value) || 8)
                      }
                    />
                  </label>
                  <label className="block space-y-1 text-sm text-ink-500">
                    <span>视觉主题</span>
                    <select
                      className="input text-ink-900"
                      value={mediaTheme}
                      onChange={(e) =>
                        setMediaTheme(
                          e.target.value as "dark" | "light" | "warm",
                        )
                      }
                    >
                      <option value="dark">深色</option>
                      <option value="light">浅色</option>
                      <option value="warm">暖色</option>
                    </select>
                  </label>
                </div>
                <label className="flex items-center gap-2 text-sm text-ink-500">
                  <input
                    type="checkbox"
                    checked={mediaVoice}
                    onChange={(e) => setMediaVoice(e.target.checked)}
                  />
                  使用 edge-tts 生成配音与字幕
                </label>
              </>
            ) : (
              <>
                <label className="block space-y-1 text-sm text-ink-500">
                  <span>口播稿</span>
                  <textarea
                    className="input text-ink-900"
                    rows={6}
                    maxLength={2000}
                    required
                    value={mediaText}
                    onChange={(e) => setMediaText(e.target.value)}
                    placeholder="粘贴口播稿，最多 2000 字"
                  />
                </label>
                <label className="block space-y-1 text-sm text-ink-500">
                  <span>语速</span>
                  <select
                    className="input text-ink-900"
                    value={mediaRate}
                    onChange={(e) => setMediaRate(e.target.value)}
                  >
                    <option value="-10%">慢 10%</option>
                    <option value="+0%">正常</option>
                    <option value="+10%">快 10%</option>
                    <option value="+20%">快 20%</option>
                  </select>
                </label>
              </>
            )}
            <label className="block space-y-1 text-sm text-ink-500">
              <span>标题（可选）</span>
              <input
                className="input text-ink-900"
                maxLength={120}
                value={mediaTitle}
                onChange={(e) => setMediaTitle(e.target.value)}
                placeholder="留空则使用模型标题"
              />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block space-y-1 text-sm text-ink-500">
                <span>归属平台</span>
                <select
                  className="input text-ink-900"
                  value={mediaPlatform}
                  onChange={(e) => setMediaPlatform(e.target.value)}
                >
                  {PLATFORMS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block space-y-1 text-sm text-ink-500">
                <span>音色</span>
                <select
                  className="input text-ink-900"
                  value={mediaVoiceName}
                  onChange={(e) => setMediaVoiceName(e.target.value)}
                >
                  {VOICES.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="text-xs text-ink-500">
              生成任务会进入后台队列（需要运行 make worker），完成后自动刷新；
              配音还需访问 edge-tts 网络。
            </p>
            <button
              className="btn-primary w-full"
              disabled={mediaJobBusy}
            >
              {mediaJobBusy ? "生成中…" : "生成媒体"}
            </button>
            {mediaNotice && (
              <p
                className={
                  mediaNotice.kind === "ok"
                    ? "text-sm text-sage-600"
                    : "text-sm text-wine-600"
                }
              >
                {mediaNotice.text}
              </p>
            )}
            {activeMediaJob && activeMediaJob.status !== "succeeded" && (
              <div className="rounded border border-line bg-white p-3 text-sm">
                <p className="text-ink-700">
                  媒体任务：{mediaJobStatusLabel(activeMediaJob.status)}
                  {activeMediaJob.attempts > 1
                    ? `（第 ${activeMediaJob.attempts} 次尝试）`
                    : ""}
                </p>
                {activeMediaJob.status === "failed" && (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <span className="text-wine-600">
                      {activeMediaJob.error_message || "生成失败"}
                    </span>
                    <button
                      type="button"
                      className="rounded border border-line px-3 py-1 text-xs text-ink-700 hover:border-ink-300 disabled:opacity-50"
                      disabled={!canWrite || retryMedia.isPending}
                      onClick={() => retryMedia.mutate(activeMediaJob.id)}
                    >
                      {retryMedia.isPending ? "提交中…" : "重试"}
                    </button>
                  </div>
                )}
              </div>
            )}
            {mediaJob.isError && (
              <p className="text-sm text-wine-600">
                任务状态查询失败：{errorText(mediaJob.error)}
              </p>
            )}
          </form>
          )}
        </div>

        <section className="space-y-3">
          <h2 className="font-medium">媒体产物</h2>
          {media.isLoading && (
            <p className="text-sm text-ink-500">加载中…</p>
          )}
          {media.isError && (
            <p className="text-sm text-wine-600">{errorText(media.error)}</p>
          )}
          {media.isSuccess && media.data.items.length === 0 && (
            <p className="text-sm text-ink-500">
              还没有媒体产物，先在左侧生成短视频或配音。
            </p>
          )}
          {media.data?.items.map((item) => (
            <article
              key={item.content_id}
              className="card p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h3 className="font-medium">{item.title || "未命名媒体"}</h3>
                  <p className="mt-1 text-xs text-ink-500">
                    {platformLabel(item.platform)} ·{" "}
                    {contentKindLabel(item.kind)}
                    {item.duration_seconds > 0
                      ? ` · ${formatDuration(item.duration_seconds)}`
                      : ""}
                    {` · ${new Date(item.created_at).toLocaleString()}`}
                  </p>
                </div>
                {item.preview && (
                  <button
                    type="button"
                    className="rounded border border-line px-3 py-1.5 text-sm text-ink-700 hover:border-ink-300"
                    onClick={() =>
                      setOpenMedia(
                        openMedia === item.content_id ? "" : item.content_id,
                      )
                    }
                  >
                    {openMedia === item.content_id ? "收起预览" : "预览"}
                  </button>
                )}
              </div>
              {item.audio.status === "failed" && (
                <p className="mt-2 text-xs text-warm-600">
                  配音生成失败，已输出静音视频。
                </p>
              )}
              {item.audio.llm_fallback && (
                <p className="mt-2 text-xs text-warm-600">
                  LLM 调用失败（{item.audio.llm_fallback}
                  ），已直接使用原始口播稿完成配音。
                </p>
              )}
              <p className="mt-2 text-xs text-ink-500">
                文件：
                {item.files.map((file) => mediaFileName(file.path)).join("、")}
              </p>
              {openMedia === item.content_id && item.preview && (
                <div className="mt-3 space-y-3 border-t border-line pt-3">
                  <MediaPreview
                    projectId={projectId}
                    path={item.preview}
                    title={item.title}
                  />
                  {item.subtitles && (
                    <details>
                      <summary className="cursor-pointer text-xs text-sage-700">
                        查看字幕
                      </summary>
                      <div className="mt-2">
                        <MediaPreview
                          projectId={projectId}
                          path={item.subtitles}
                        />
                      </div>
                    </details>
                  )}
                </div>
              )}
            </article>
          ))}
        </section>

        <section className="space-y-3">
          <h2 className="font-medium">内容列表</h2>
          {contents.isLoading && (
            <p className="text-sm text-ink-500">加载中…</p>
          )}
          {contents.isError && (
            <p className="text-sm text-wine-600">
              {errorText(contents.error)}
            </p>
          )}
          {accounts.isError && (
            <p className="text-sm text-wine-600">{errorText(accounts.error)}</p>
          )}
          {contents.isSuccess && contents.data.length === 0 && (
            <p className="text-sm text-ink-500">
              还没有内容，先在左侧创建。
            </p>
          )}
          {jobNotice && (
            <p
              className={
                jobNotice.kind === "ok"
                  ? "text-sm text-sage-600"
                  : "text-sm text-wine-600"
              }
            >
              {jobNotice.text}
              {jobNotice.kind === "ok" && (
                <Link className="ml-2 text-sage-700" to="/publish">
                  前往发布
                </Link>
              )}
            </p>
          )}
          {contents.data?.map((content) => {
            const pending =
              createJob.isPending &&
              createJob.variables?.content.id === content.id;
            const contentSpec = specFor(content.platform, platformSpecs);
            const capability = contentSpec
              ? capabilityFor(contentSpec, content.kind)
              : "pending";
            const needsAccount = capability === "auto_publish";
            const platformAccounts = accountsForPlatform(
              accounts.data ?? [],
              content.platform,
            );
            const selectedAccountId =
              accountSelections[content.id] ?? platformAccounts[0]?.id ?? "";
            const missingAccount =
              needsAccount && accounts.isSuccess && !selectedAccountId;
            const accountBlocked =
              needsAccount && (!accounts.isSuccess || !selectedAccountId);
            return (
              <article
                key={content.id}
                className="card p-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <h3 className="font-medium">
                      {content.title || "未命名内容"}
                    </h3>
                    <p className="mt-1 text-xs text-ink-500">
                      {platformLabel(content.platform)} ·{" "}
                      {contentKindLabel(content.kind)} ·{" "}
                      {contentStatusLabel(content.status)} · v{content.current_version}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      className="rounded border border-line px-3 py-1.5 text-sm text-ink-700 hover:border-ink-300"
                      onClick={() =>
                        setOpenVersions(
                          openVersions === content.id ? "" : content.id,
                        )
                      }
                    >
                      {openVersions === content.id ? "收起版本" : "查看版本"}
                    </button>
                    <button
                      type="button"
                      className="btn-primary px-3 py-1.5"
                      disabled={
                        !canWrite ||
                        createJob.isPending ||
                        content.current_version < 1 ||
                        accountBlocked
                      }
                      title={
                        !canWrite
                          ? "只读成员不能创建发布任务"
                          : content.current_version < 1
                            ? "内容还没有版本，无法发布"
                            : accountBlocked
                              ? accounts.isSuccess
                                ? `请先授权${contentSpec?.label ?? "平台"}账号`
                                : "正在加载发布账号"
                              : undefined
                      }
                      onClick={() => {
                        setJobNotice(null);
                        createJob.mutate({
                          content,
                          accountId: needsAccount ? selectedAccountId : "",
                        });
                      }}
                    >
                      {pending ? "创建中…" : "创建发布任务"}
                    </button>
                  </div>
                </div>

                {needsAccount && (
                  <div className="mt-3 space-y-1">
                    <label className="block text-xs text-ink-500">
                      发布账号
                      <select
                        className="mt-1 input text-sm text-ink-900 disabled:opacity-50"
                        value={selectedAccountId}
                        onChange={(e) =>
                          setAccountSelections((prev) => ({
                            ...prev,
                            [content.id]: e.target.value,
                          }))
                        }
                        disabled={
                          !accounts.isSuccess || platformAccounts.length === 0
                        }
                      >
                        {!accounts.isSuccess && (
                          <option value="">
                            {accounts.isError ? "账号加载失败" : "加载中…"}
                          </option>
                        )}
                        {accounts.isSuccess && platformAccounts.length === 0 && (
                          <option value="">暂无可用账号</option>
                        )}
                        {platformAccounts.map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    {missingAccount && (
                      <p className="text-xs text-warm-600">
                        还没有可用的{contentSpec?.label ?? "平台"}账号，请先
                        <a className="ml-1 text-sage-700" href="/accounts">
                          添加账号
                        </a>
                      </p>
                    )}
                  </div>
                )}

                {capability === "pending" && (
                  <p className="mt-2 text-xs text-warm-600">
                    {contentSpec?.note ?? "该平台暂不支持自动发布或导出"}
                  </p>
                )}

                {openVersions === content.id && (
                  <div className="mt-4 space-y-2 border-t border-line pt-3">
                    {versions.isLoading && (
                      <p className="text-sm text-ink-500">版本加载中…</p>
                    )}
                    {versions.isError && (
                      <p className="text-sm text-wine-600">
                        {errorText(versions.error)}
                      </p>
                    )}
                    {versions.data?.items.map((version) => (
                      <div
                        key={version.id}
                        className="rounded border border-line bg-white p-3 text-sm"
                      >
                        <p className="flex flex-wrap gap-x-3 text-xs text-ink-500">
                          <span>v{version.version}</span>
                          <span>来源：{version.source}</span>
                        </p>
                        <p className="mt-1 font-medium">
                          {version.title || "无标题"}
                        </p>
                        <p className="mt-1 whitespace-pre-wrap text-ink-700">
                          {version.body || "（空正文）"}
                        </p>
                      </div>
                    ))}
                    {versions.isSuccess && versions.data.items.length === 0 && (
                      <p className="text-sm text-ink-500">暂无版本。</p>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </section>
      </div>
    </div>
  );
}
