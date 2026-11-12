import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useAuth } from "../auth-context";
import { api } from "../client";
import { errorText, platformLabel, type Account } from "../types";

type Feedback = { kind: "ok" | "error"; text: string };
type RowFeedback = Feedback & { id: string };

const EMPTY_FORM = {
  name: "",
  appId: "",
  appSecret: "",
  author: "",
  thumbMediaId: "",
  needOpenComment: false,
  onlyFansCanComment: false,
};

function isAscii(value: string): boolean {
  return /^[\x20-\x7E]+$/.test(value);
}

function statusLabel(status: string): string {
  if (status === "active") return "已授权";
  if (status === "revoked") return "已停用";
  return status;
}

export default function Accounts() {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const [form, setForm] = useState(EMPTY_FORM);
  const [message, setMessage] = useState<Feedback | null>(null);
  const [rowFeedback, setRowFeedback] = useState<RowFeedback | null>(null);
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(
    null,
  );
  const [credentialFor, setCredentialFor] = useState("");
  const [credentialForm, setCredentialForm] = useState({
    appId: "",
    appSecret: "",
  });
  const [revokingId, setRevokingId] = useState("");

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Account[]>("/accounts"),
  });

  const create = useMutation({
    mutationFn: (payload: typeof EMPTY_FORM) =>
      api<Account>("/accounts", {
        method: "POST",
        body: JSON.stringify({
          platform: "wechat",
          name: payload.name,
          credential: {
            app_id: payload.appId,
            app_secret: payload.appSecret,
          },
          meta: {
            thumb_media_id: payload.thumbMediaId,
            need_open_comment: payload.needOpenComment ? 1 : 0,
            only_fans_can_comment: payload.onlyFansCanComment ? 1 : 0,
            ...(payload.author ? { author: payload.author } : {}),
          },
        }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setForm(EMPTY_FORM);
      setMessage({ kind: "ok", text: "公众号账号已授权并保存" });
    },
    onError: (err) =>
      setMessage({ kind: "error", text: errorText(err) }),
  });

  const rename = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      api<Account>(`/accounts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    onSuccess: (_account, variables) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setEditing(null);
      setRowFeedback({
        id: variables.id,
        kind: "ok",
        text: "名称已更新",
      });
    },
    onError: (err, variables) =>
      setRowFeedback({
        id: variables.id,
        kind: "error",
        text: errorText(err),
      }),
  });

  const updateCredential = useMutation({
    mutationFn: ({
      id,
      appId,
      appSecret,
    }: {
      id: string;
      appId: string;
      appSecret: string;
    }) =>
      api<Account>(`/accounts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({
          credential: { app_id: appId, app_secret: appSecret },
        }),
      }),
    onSuccess: (_account, variables) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setCredentialFor("");
      setCredentialForm({ appId: "", appSecret: "" });
      setRowFeedback({
        id: variables.id,
        kind: "ok",
        text: "凭据已更新",
      });
    },
    onError: (err, variables) =>
      setRowFeedback({
        id: variables.id,
        kind: "error",
        text: errorText(err),
      }),
  });

  const revoke = useMutation({
    mutationFn: (account: Account) =>
      api<void>(`/accounts/${account.id}`, { method: "DELETE" }),
    onSuccess: (_result, account) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      if (editing?.id === account.id) setEditing(null);
      if (credentialFor === account.id) setCredentialFor("");
      setRevokingId("");
      setRowFeedback({
        id: account.id,
        kind: "ok",
        text: "账号已停用，凭据已销毁",
      });
    },
    onError: (err, account) =>
      setRowFeedback({
        id: account.id,
        kind: "error",
        text: errorText(err),
      }),
  });

  function submitCreate(event: FormEvent) {
    event.preventDefault();
    const name = form.name.trim();
    const appId = form.appId.trim();
    const appSecret = form.appSecret.trim();
    const thumbMediaId = form.thumbMediaId.trim();
    if (!name || !appId || !appSecret || !thumbMediaId) {
      setMessage({
        kind: "error",
        text: "请填写名称、AppID、AppSecret 和封面素材 ID",
      });
      return;
    }
    if (!isAscii(appId) || !isAscii(appSecret)) {
      setMessage({ kind: "error", text: "AppID 与 AppSecret 只能包含 ASCII 字符" });
      return;
    }
    if (appSecret.length < 8) {
      setMessage({ kind: "error", text: "AppSecret 至少 8 个字符" });
      return;
    }
    setMessage(null);
    create.mutate({
      ...form,
      name,
      appId,
      appSecret,
      thumbMediaId,
      author: form.author.trim(),
    });
  }

  function submitRename(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    const name = editing.name.trim();
    if (!name) {
      setRowFeedback({ id: editing.id, kind: "error", text: "名称不能为空" });
      return;
    }
    setRowFeedback(null);
    rename.mutate({ id: editing.id, name });
  }

  function submitCredential(event: FormEvent) {
    event.preventDefault();
    if (!credentialFor) return;
    const appId = credentialForm.appId.trim();
    const appSecret = credentialForm.appSecret.trim();
    if (!appId || !appSecret) {
      setRowFeedback({
        id: credentialFor,
        kind: "error",
        text: "AppID 与 AppSecret 需要同时填写",
      });
      return;
    }
    if (!isAscii(appId) || !isAscii(appSecret)) {
      setRowFeedback({
        id: credentialFor,
        kind: "error",
        text: "AppID 与 AppSecret 只能包含 ASCII 字符",
      });
      return;
    }
    if (appSecret.length < 8) {
      setRowFeedback({
        id: credentialFor,
        kind: "error",
        text: "AppSecret 至少 8 个字符",
      });
      return;
    }
    setRowFeedback(null);
    updateCredential.mutate({
      id: credentialFor,
      appId,
      appSecret,
    });
  }

  function revokeAccount(account: Account) {
    setRowFeedback(null);
    setRevokingId("");
    revoke.mutate(account);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">发布账号</h1>
        <p className="mt-1 text-sm text-slate-400">
          授权公众号账号后，发布任务才能创建草稿。仅在官方接口使用，凭据加密存储且只返回掩码。
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[380px_1fr]">
        {isAdmin && (
          <form
            className="space-y-3 self-start rounded-xl border border-slate-800 bg-slate-900 p-5"
            onSubmit={submitCreate}
          >
          <h2 className="font-medium">授权公众号</h2>
          <label className="block space-y-1 text-sm text-slate-400">
            <span>账号名称 *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={100}
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="例如：主号"
            />
          </label>
          <label className="block space-y-1 text-sm text-slate-400">
            <span>AppID *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={64}
              required
              value={form.appId}
              onChange={(e) => setForm({ ...form, appId: e.target.value })}
              placeholder="公众号 AppID"
            />
          </label>
          <label className="block space-y-1 text-sm text-slate-400">
            <span>AppSecret *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              type="password"
              minLength={8}
              maxLength={512}
              required
              value={form.appSecret}
              onChange={(e) => setForm({ ...form, appSecret: e.target.value })}
              placeholder="公众号 AppSecret"
            />
          </label>
          <label className="block space-y-1 text-sm text-slate-400">
            <span>封面素材 ID *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={200}
              required
              value={form.thumbMediaId}
              onChange={(e) =>
                setForm({ ...form, thumbMediaId: e.target.value })
              }
              placeholder="thumb_media_id"
            />
            <span className="block text-xs text-slate-500">
              在公众号素材库上传封面后获取的永久素材 ID；草稿缺少封面会发布失败。
            </span>
          </label>
          <label className="block space-y-1 text-sm text-slate-400">
            <span>作者（可选）</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={64}
              value={form.author}
              onChange={(e) => setForm({ ...form, author: e.target.value })}
              placeholder="显示在文章署名处"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={form.needOpenComment}
              onChange={(e) =>
                setForm({ ...form, needOpenComment: e.target.checked })
              }
            />
            允许读者评论
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={form.onlyFansCanComment}
              onChange={(e) =>
                setForm({ ...form, onlyFansCanComment: e.target.checked })
              }
            />
            仅关注者可评论
          </label>
          {message && (
            <p
              className={
                message.kind === "ok"
                  ? "text-sm text-emerald-400"
                  : "text-sm text-red-400"
              }
            >
              {message.text}
            </p>
          )}
          <button
            className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
            disabled={create.isPending}
          >
            {create.isPending ? "保存中…" : "保存授权"}
          </button>
        </form>
        )}

        <section className="space-y-3">
          <h2 className="font-medium">已授权账号</h2>
          {accounts.isLoading && (
            <p className="text-sm text-slate-400">加载中…</p>
          )}
          {accounts.isError && (
            <p className="text-sm text-red-400">{errorText(accounts.error)}</p>
          )}
          {accounts.isSuccess && accounts.data.length === 0 && (
            <p className="rounded-xl border border-slate-800 bg-slate-900 p-6 text-sm text-slate-400">
              还没有公众号账号，先在左侧完成授权。
            </p>
          )}
          {accounts.data?.map((account) => {
            const active = account.status === "active";
            const feedback =
              rowFeedback?.id === account.id ? rowFeedback : null;
            return (
              <article
                key={account.id}
                className="rounded-xl border border-slate-800 bg-slate-900 p-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <h3 className="font-medium">{account.name}</h3>
                    <p className="mt-1 text-xs text-slate-500">
                      {platformLabel(account.platform)}
                    </p>
                  </div>
                  <span
                    className={
                      active
                        ? "text-xs text-emerald-400"
                        : "text-xs text-slate-500"
                    }
                  >
                    {statusLabel(account.status)}
                  </span>
                </div>
                <p className="mt-2 text-xs text-slate-500">
                  凭据：
                  <span className="ml-1 font-mono">
                    {account.credential_masked === "{}"
                      ? "已销毁"
                      : account.credential_masked}
                  </span>
                </p>

                {active && isAdmin && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      type="button"
                      className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-600"
                      onClick={() => {
                        setEditing({ id: account.id, name: account.name });
                        setCredentialFor("");
                        setRowFeedback(null);
                      }}
                    >
                      重命名
                    </button>
                    <button
                      type="button"
                      className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-600"
                      onClick={() => {
                        setCredentialFor(account.id);
                        setCredentialForm({ appId: "", appSecret: "" });
                        setEditing(null);
                        setRowFeedback(null);
                      }}
                    >
                      更新凭据
                    </button>
                    {revokingId === account.id ? (
                      <span className="flex flex-wrap items-center gap-2 rounded border border-red-900 px-3 py-1.5 text-sm text-red-300">
                        停用后凭据销毁且无法恢复
                        <button
                          type="button"
                          className="rounded bg-red-700 px-2 py-1 text-xs font-medium text-white hover:bg-red-600 disabled:opacity-50"
                          disabled={revoke.isPending}
                          onClick={() => revokeAccount(account)}
                        >
                          {revoke.isPending ? "停用中…" : "确认停用"}
                        </button>
                        <button
                          type="button"
                          className="rounded px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
                          onClick={() => setRevokingId("")}
                        >
                          取消
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="rounded border border-red-900 px-3 py-1.5 text-sm text-red-400 hover:border-red-800 disabled:opacity-50"
                        onClick={() => {
                          setRevokingId(account.id);
                          setEditing(null);
                          setCredentialFor("");
                          setRowFeedback(null);
                        }}
                      >
                        停用
                      </button>
                    )}
                  </div>
                )}

                {editing?.id === account.id && (
                  <form
                    className="mt-3 flex flex-wrap items-end gap-2 border-t border-slate-800 pt-3"
                    onSubmit={submitRename}
                  >
                    <label className="min-w-48 flex-1 space-y-1 text-sm text-slate-400">
                      <span>新名称</span>
                      <input
                        className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                        maxLength={100}
                        required
                        value={editing.name}
                        onChange={(e) =>
                          setEditing({ ...editing, name: e.target.value })
                        }
                      />
                    </label>
                    <button
                      className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium disabled:opacity-50"
                      disabled={rename.isPending}
                    >
                      {rename.isPending ? "保存中…" : "保存"}
                    </button>
                    <button
                      type="button"
                      className="rounded border border-slate-700 px-4 py-2 text-sm text-slate-300"
                      onClick={() => setEditing(null)}
                    >
                      取消
                    </button>
                  </form>
                )}

                {credentialFor === account.id && (
                  <form
                    className="mt-3 space-y-2 border-t border-slate-800 pt-3"
                    onSubmit={submitCredential}
                  >
                    <p className="text-xs text-slate-500">
                      更新凭据需同时填写新的 AppID 与 AppSecret。
                    </p>
                    <div className="flex flex-wrap items-end gap-2">
                      <label className="min-w-48 flex-1 space-y-1 text-sm text-slate-400">
                        <span>AppID</span>
                        <input
                          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                          maxLength={64}
                          required
                          value={credentialForm.appId}
                          onChange={(e) =>
                            setCredentialForm({
                              ...credentialForm,
                              appId: e.target.value,
                            })
                          }
                        />
                      </label>
                      <label className="min-w-48 flex-1 space-y-1 text-sm text-slate-400">
                        <span>AppSecret</span>
                        <input
                          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                          type="password"
                          minLength={8}
                          maxLength={512}
                          required
                          value={credentialForm.appSecret}
                          onChange={(e) =>
                            setCredentialForm({
                              ...credentialForm,
                              appSecret: e.target.value,
                            })
                          }
                        />
                      </label>
                      <button
                        className="rounded bg-indigo-600 px-4 py-2 text-sm font-medium disabled:opacity-50"
                        disabled={updateCredential.isPending}
                      >
                        {updateCredential.isPending ? "保存中…" : "保存"}
                      </button>
                      <button
                        type="button"
                        className="rounded border border-slate-700 px-4 py-2 text-sm text-slate-300"
                        onClick={() => {
                          setCredentialFor("");
                          setCredentialForm({ appId: "", appSecret: "" });
                        }}
                      >
                        取消
                      </button>
                    </div>
                  </form>
                )}

                {feedback && (
                  <p
                    className={
                      feedback.kind === "ok"
                        ? "mt-3 text-sm text-emerald-400"
                        : "mt-3 text-sm text-red-400"
                    }
                  >
                    {feedback.text}
                  </p>
                )}
              </article>
            );
          })}
        </section>
      </div>
    </div>
  );
}
