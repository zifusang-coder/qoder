import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../client";
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
  type Project,
  type RepurposeResult,
  type VersionList,
} from "../types";

type Notice = { kind: "ok" | "error"; text: string };

export default function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const projectId = id ?? "";
  const qc = useQueryClient();

  const [platform, setPlatform] = useState("wechat");
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

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Account[]>("/accounts"),
  });
  const wechatAccounts = (accounts.data ?? []).filter(
    (account) => account.platform === "wechat" && account.status === "active",
  );

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

  function submitContent(event: FormEvent) {
    event.preventDefault();
    createContent.mutate({
      platform,
      kind: "article",
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

  const effectiveSource = sourceId || contents.data?.[0]?.id || "";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link className="text-sm text-indigo-400" to="/projects">
            ← 返回项目
          </Link>
          <h1 className="mt-2 text-xl font-semibold">
            {project.data?.title ?? "项目详情"}
          </h1>
          {project.data?.topic && (
            <p className="mt-1 text-sm text-slate-400">{project.data.topic}</p>
          )}
        </div>
        <span className="text-xs text-slate-500">
          {project.data ? `状态：${projectStatusLabel(project.data.status)}` : ""}
        </span>
      </div>

      {project.isError && (
        <p className="text-sm text-red-400">{errorText(project.error)}</p>
      )}

      <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
        <div className="space-y-6">
          <form
            className="space-y-3 rounded-xl border border-slate-800 bg-slate-900 p-5"
            onSubmit={submitContent}
          >
            <h2 className="font-medium">新建内容</h2>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>平台</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                value={platform}
                onChange={(e) => setPlatform(e.target.value)}
              >
                {PLATFORMS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>标题</span>
              <input
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                maxLength={300}
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="内容标题"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>正文</span>
              <textarea
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                rows={10}
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder="粘贴或撰写正文"
              />
            </label>
            {createContent.isError && (
              <p className="text-sm text-red-400">
                {errorText(createContent.error)}
              </p>
            )}
            <button
              className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
              disabled={createContent.isPending}
            >
              {createContent.isPending ? "创建中…" : "创建内容"}
            </button>
          </form>

          <form
            className="space-y-3 rounded-xl border border-slate-800 bg-slate-900 p-5"
            onSubmit={submitRepurpose}
          >
            <h2 className="font-medium">一稿多改</h2>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>源内容</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
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
            <label className="block space-y-1 text-sm text-slate-400">
              <span>目标平台</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
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
            <p className="text-xs text-slate-500">
              将调用 content-repurposing 技能生成目标平台版本。
            </p>
            <button
              className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
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
                    ? "text-sm text-emerald-400"
                    : "text-sm text-red-400"
                }
              >
                {repurposeNotice.text}
              </p>
            )}
          </form>
        </div>

        <section className="space-y-3">
          <h2 className="font-medium">内容列表</h2>
          {contents.isLoading && (
            <p className="text-sm text-slate-400">加载中…</p>
          )}
          {contents.isError && (
            <p className="text-sm text-red-400">
              {errorText(contents.error)}
            </p>
          )}
          {accounts.isError && (
            <p className="text-sm text-red-400">{errorText(accounts.error)}</p>
          )}
          {contents.isSuccess && contents.data.length === 0 && (
            <p className="text-sm text-slate-400">
              还没有内容，先在左侧创建。
            </p>
          )}
          {jobNotice && (
            <p
              className={
                jobNotice.kind === "ok"
                  ? "text-sm text-emerald-400"
                  : "text-sm text-red-400"
              }
            >
              {jobNotice.text}
              {jobNotice.kind === "ok" && (
                <Link className="ml-2 text-indigo-400" to="/publish">
                  前往发布
                </Link>
              )}
            </p>
          )}
          {contents.data?.map((content) => {
            const pending =
              createJob.isPending &&
              createJob.variables?.content.id === content.id;
            const needsAccount = content.platform === "wechat";
            const selectedAccountId =
              accountSelections[content.id] ?? wechatAccounts[0]?.id ?? "";
            const missingAccount =
              needsAccount && accounts.isSuccess && !selectedAccountId;
            const accountBlocked =
              needsAccount && (!accounts.isSuccess || !selectedAccountId);
            return (
              <article
                key={content.id}
                className="rounded-xl border border-slate-800 bg-slate-900 p-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <h3 className="font-medium">
                      {content.title || "未命名内容"}
                    </h3>
                    <p className="mt-1 text-xs text-slate-500">
                      {platformLabel(content.platform)} ·{" "}
                      {contentKindLabel(content.kind)} ·{" "}
                      {contentStatusLabel(content.status)} · v{content.current_version}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-600"
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
                      className="rounded bg-indigo-600 px-3 py-1.5 text-sm font-medium disabled:opacity-50"
                      disabled={
                        createJob.isPending ||
                        content.current_version < 1 ||
                        accountBlocked
                      }
                      title={
                        content.current_version < 1
                          ? "内容还没有版本，无法发布"
                          : accountBlocked
                            ? accounts.isSuccess
                              ? "请先授权公众号账号"
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
                    <label className="block text-xs text-slate-500">
                      发布账号
                      <select
                        className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 disabled:opacity-50"
                        value={selectedAccountId}
                        onChange={(e) =>
                          setAccountSelections((prev) => ({
                            ...prev,
                            [content.id]: e.target.value,
                          }))
                        }
                        disabled={
                          !accounts.isSuccess || wechatAccounts.length === 0
                        }
                      >
                        {!accounts.isSuccess && (
                          <option value="">
                            {accounts.isError ? "账号加载失败" : "加载中…"}
                          </option>
                        )}
                        {accounts.isSuccess && wechatAccounts.length === 0 && (
                          <option value="">暂无可用账号</option>
                        )}
                        {wechatAccounts.map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    {missingAccount && (
                      <p className="text-xs text-amber-400">
                        还没有可用的公众号账号，请先
                        <Link className="ml-1 text-indigo-400" to="/accounts">
                          授权账号
                        </Link>
                        。
                      </p>
                    )}
                  </div>
                )}

                {openVersions === content.id && (
                  <div className="mt-4 space-y-2 border-t border-slate-800 pt-3">
                    {versions.isLoading && (
                      <p className="text-sm text-slate-400">版本加载中…</p>
                    )}
                    {versions.isError && (
                      <p className="text-sm text-red-400">
                        {errorText(versions.error)}
                      </p>
                    )}
                    {versions.data?.items.map((version) => (
                      <div
                        key={version.id}
                        className="rounded border border-slate-800 bg-slate-950 p-3 text-sm"
                      >
                        <p className="flex flex-wrap gap-x-3 text-xs text-slate-500">
                          <span>v{version.version}</span>
                          <span>来源：{version.source}</span>
                        </p>
                        <p className="mt-1 font-medium">
                          {version.title || "无标题"}
                        </p>
                        <p className="mt-1 whitespace-pre-wrap text-slate-300">
                          {version.body || "（空正文）"}
                        </p>
                      </div>
                    ))}
                    {versions.isSuccess && versions.data.items.length === 0 && (
                      <p className="text-sm text-slate-400">暂无版本。</p>
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
