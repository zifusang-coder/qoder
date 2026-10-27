import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../client";
import {
  errorText,
  projectStatusLabel,
  type ProfileList,
  type Project,
  type ProjectList,
} from "../types";

export default function Projects() {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [topic, setTopic] = useState("");
  const [profileId, setProfileId] = useState("");
  const [createdId, setCreatedId] = useState("");
  const [message, setMessage] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);

  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api<ProfileList>("/profiles"),
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<ProjectList>("/projects"),
  });

  const createProject = useMutation({
    mutationFn: (payload: {
      title: string;
      topic: string;
      profile_id: string | null;
    }) =>
      api<Project>("/projects", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (project, payload) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setCreatedId(project.id);
      setTitle((prev) => (prev === payload.title ? "" : prev));
      setTopic((prev) => (prev === payload.topic ? "" : prev));
      setProfileId((prev) =>
        prev === (payload.profile_id ?? "") ? "" : prev,
      );
      setMessage({ kind: "ok", text: `已创建项目「${project.title}」` });
    },
    onError: () => setMessage(null),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = title.trim();
    if (!trimmed) {
      setMessage({ kind: "error", text: "请填写项目标题" });
      return;
    }
    setCreatedId("");
    setMessage(null);
    createProject.mutate({
      title: trimmed,
      topic: topic.trim(),
      profile_id: profileId || null,
    });
  }

  const profileNames = new Map(
    (profiles.data?.items ?? []).map((profile) => [profile.id, profile.name]),
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">项目</h1>
        <p className="mt-1 text-sm text-slate-400">
          一个项目对应一个选题，管理多平台内容与版本。
        </p>
      </div>

      <form
        className="grid gap-4 rounded-xl border border-slate-800 bg-slate-900 p-6 md:grid-cols-[1fr_1fr_180px_auto]"
        onSubmit={submit}
      >
        <label className="space-y-1 text-sm text-slate-400">
          <span>标题 *</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            maxLength={200}
            required
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="例如：AI 写作工作流"
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>选题</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="核心角度或选题说明"
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>绑定画像</span>
          <select
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={profileId}
            onChange={(e) => setProfileId(e.target.value)}
          >
            <option value="">不绑定</option>
            {profiles.data?.items.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.name}
              </option>
            ))}
          </select>
        </label>
        {profiles.isError && (
          <p className="text-xs text-red-400">{errorText(profiles.error)}</p>
        )}
        <div className="flex items-end">
          <button
            className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
            disabled={createProject.isPending}
          >
            {createProject.isPending ? "创建中…" : "创建项目"}
          </button>
        </div>
      </form>

      {(createProject.isError || message) && (
        <p
          className={
            createProject.isError || message?.kind === "error"
              ? "text-sm text-red-400"
              : "text-sm text-emerald-400"
          }
        >
          {createProject.isError
            ? errorText(createProject.error)
            : message?.text}
          {createdId && !createProject.isError && message?.kind === "ok" && (
            <Link className="ml-2 text-indigo-400" to={`/projects/${createdId}`}>
              打开项目
            </Link>
          )}
        </p>
      )}

      <section className="space-y-2">
        <h2 className="font-medium">全部项目</h2>
        {projects.isLoading && (
          <p className="text-sm text-slate-400">加载中…</p>
        )}
        {projects.isError && (
          <p className="text-sm text-red-400">
            {errorText(projects.error)}
          </p>
        )}
        {projects.isSuccess && projects.data.items.length === 0 && (
          <p className="text-sm text-slate-400">还没有项目。</p>
        )}
        {projects.data?.items.map((project) => (
          <Link
            key={project.id}
            to={`/projects/${project.id}`}
            className="block rounded-xl border border-slate-800 bg-slate-900 p-4 transition-colors hover:border-slate-700"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{project.title}</span>
              <span className="text-xs text-slate-500">
                {projectStatusLabel(project.status)}
              </span>
            </div>
            <p className="mt-1 text-sm text-slate-400">
              {project.topic || "未填写选题"}
            </p>
            <p className="mt-2 text-xs text-slate-500">
              画像：
              {project.profile_id
                ? profileNames.get(project.profile_id) ?? "未知画像"
                : "未绑定"}
              {project.platforms.length > 0 && (
                <span className="ml-3">
                  平台：{project.platforms.join("、")}
                </span>
              )}
            </p>
          </Link>
        ))}
      </section>
    </div>
  );
}
