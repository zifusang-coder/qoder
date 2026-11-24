import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useAuth } from "../auth-context";
import { api } from "../client";
import {
  CAPABILITY_LABELS,
  FALLBACK_PLATFORMS,
  type Capability,
  type CredentialField,
  type PlatformSpec,
  specFor,
  usePlatforms,
} from "../platforms";
import { errorText, type Account } from "../types";

type Feedback = { kind: "ok" | "error"; text: string };
type RowFeedback = Feedback & { id: string };

function defaultMeta(
  platform: string,
): Record<string, string | number | boolean> {
  if (platform === "wechat") {
    return {
      author: "",
      thumb_media_id: "",
      need_open_comment: false,
      only_fans_can_comment: false,
    };
  }
  if (platform === "toutiao") return { author: "" };
  return {};
}

function statusLabel(status: string): string {
  if (status === "active") return "已授权";
  if (status === "revoked") return "已停用";
  return status;
}

function capabilityClass(capability: string): string {
  if (capability === "auto_publish") {
    return "bg-mint-50 text-mint-700";
  }
  if (capability === "manual_export") {
    return "bg-brand-50 text-brand-700";
  }
  return "bg-brand-50 text-ink-500";
}

function credentialPayload(
  fields: CredentialField[],
  values: Record<string, string>,
): Record<string, string> {
  const payload: Record<string, string> = {};
  for (const field of fields) {
    const value = (values[field.key] ?? "").trim();
    if (value) payload[field.key] = value;
  }
  return payload;
}

function metaPayload(
  platform: string,
  meta: Record<string, string | number | boolean>,
): Record<string, string | number> {
  if (platform === "wechat") {
    const payload: Record<string, string | number> = {
      thumb_media_id: String(meta.thumb_media_id ?? "").trim(),
      need_open_comment: meta.need_open_comment ? 1 : 0,
      only_fans_can_comment: meta.only_fans_can_comment ? 1 : 0,
    };
    const author = String(meta.author ?? "").trim();
    if (author) payload.author = author;
    return payload;
  }
  if (platform === "toutiao") {
    const author = String(meta.author ?? "").trim();
    return author ? { author } : {};
  }
  return {};
}

export default function Accounts() {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const platformsQuery = usePlatforms();
  const specs = platformsQuery.data ?? FALLBACK_PLATFORMS;
  const [platform, setPlatform] = useState("wechat");
  const spec = specFor(platform, specs) ?? FALLBACK_PLATFORMS[0];
  const [name, setName] = useState("");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [meta, setMeta] = useState(defaultMeta("wechat"));
  const [message, setMessage] = useState<Feedback | null>(null);
  const [rowFeedback, setRowFeedback] = useState<RowFeedback | null>(null);
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(
    null,
  );
  const [credentialFor, setCredentialFor] = useState("");
  const [credentialForm, setCredentialForm] = useState<Record<string, string>>(
    {},
  );
  const [revokingId, setRevokingId] = useState("");

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: () => api<Account[]>("/accounts"),
  });

  const create = useMutation({
    mutationFn: (payload: {
      platform: string;
      name: string;
      credential: Record<string, string>;
      meta: Record<string, string | number>;
    }) =>
      api<Account>("/accounts", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setName("");
      setCredentials({});
      setMeta(defaultMeta(platform));
      setMessage({ kind: "ok", text: "账号已保存" });
    },
    onError: (err) => setMessage({ kind: "error", text: errorText(err) }),
  });

  const rename = useMutation({
    mutationFn: ({ id, newName }: { id: string; newName: string }) =>
      api<Account>(`/accounts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ name: newName }),
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
      credential,
    }: {
      id: string;
      credential: Record<string, string>;
    }) =>
      api<Account>(`/accounts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ credential }),
      }),
    onSuccess: (_account, variables) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setCredentialFor("");
      setCredentialForm({});
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

  function selectPlatform(next: string) {
    setPlatform(next);
    setCredentials({});
    setMeta(defaultMeta(next));
    setMessage(null);
  }

  function submitCreate(event: FormEvent) {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) {
      setMessage({ kind: "error", text: "请填写账号名称" });
      return;
    }
    for (const field of spec.credential_fields) {
      if (field.required && !(credentials[field.key] ?? "").trim()) {
        setMessage({ kind: "error", text: `请填写 ${field.label}` });
        return;
      }
    }
    if (
      platform === "wechat" &&
      !String(meta.thumb_media_id ?? "").trim()
    ) {
      setMessage({ kind: "error", text: "请填写封面素材 ID" });
      return;
    }
    setMessage(null);
    create.mutate({
      platform,
      name: trimmedName,
      credential: credentialPayload(spec.credential_fields, credentials),
      meta: metaPayload(platform, meta),
    });
  }

  function submitRename(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    const nextName = editing.name.trim();
    if (!nextName) {
      setRowFeedback({ id: editing.id, kind: "error", text: "名称不能为空" });
      return;
    }
    setRowFeedback(null);
    rename.mutate({ id: editing.id, newName: nextName });
  }

  function submitCredential(event: FormEvent, accountSpec: PlatformSpec) {
    event.preventDefault();
    if (!credentialFor) return;
    for (const field of accountSpec.credential_fields) {
      if (field.required && !(credentialForm[field.key] ?? "").trim()) {
        setRowFeedback({
          id: credentialFor,
          kind: "error",
          text: `请填写 ${field.label}`,
        });
        return;
      }
    }
    setRowFeedback(null);
    updateCredential.mutate({
      id: credentialFor,
      credential: credentialPayload(
        accountSpec.credential_fields,
        credentialForm,
      ),
    });
  }

  function revokeAccount(account: Account) {
    setRowFeedback(null);
    setRevokingId("");
    revoke.mutate(account);
  }

  const groups = specs
    .map((groupSpec) => ({
      spec: groupSpec,
      items: (accounts.data ?? []).filter(
        (account) => account.platform === groupSpec.key,
      ),
    }))
    .filter((group) => group.items.length > 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">发布账号</h1>
        <p className="mt-1 text-sm text-ink-500">
          同一平台可添加多个账号；凭据加密存储，响应只返回掩码。
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[400px_1fr]">
        {isAdmin && (
          <form
            className="space-y-3 self-start card p-5"
            onSubmit={submitCreate}
          >
            <h2 className="font-medium">添加账号</h2>
            <label className="block space-y-1 text-sm text-ink-500">
              <span>平台</span>
              <select
                className="input text-ink-900"
                value={platform}
                onChange={(e) => selectPlatform(e.target.value)}
              >
                {specs.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label} · {CAPABILITY_LABELS[item.capability]}
                  </option>
                ))}
              </select>
            </label>
            <p className="flex items-center gap-2 text-xs text-ink-500">
              <span
                className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${capabilityClass(spec.capability)}`}
              >
                {CAPABILITY_LABELS[spec.capability]}
              </span>
              {spec.note}
            </p>

            <label className="block space-y-1 text-sm text-ink-500">
              <span>账号名称 *</span>
              <input
                className="input text-ink-900"
                maxLength={100}
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：主号"
              />
            </label>

            {spec.credential_fields.map((field) => (
              <label
                key={field.key}
                className="block space-y-1 text-sm text-ink-500"
              >
                <span>
                  {field.label}
                  {field.required ? " *" : ""}
                </span>
                <input
                  className="input text-ink-900"
                  type={field.secret ? "password" : "text"}
                  required={field.required}
                  value={credentials[field.key] ?? ""}
                  onChange={(e) =>
                    setCredentials((prev) => ({
                      ...prev,
                      [field.key]: e.target.value,
                    }))
                  }
                  placeholder={field.placeholder}
                />
              </label>
            ))}

            {platform === "wechat" && (
              <>
                <label className="block space-y-1 text-sm text-ink-500">
                  <span>封面素材 ID *</span>
                  <input
                    className="input text-ink-900"
                    maxLength={200}
                    required
                    value={String(meta.thumb_media_id ?? "")}
                    onChange={(e) =>
                      setMeta({ ...meta, thumb_media_id: e.target.value })
                    }
                    placeholder="thumb_media_id"
                  />
                </label>
                <label className="block space-y-1 text-sm text-ink-500">
                  <span>作者（可选）</span>
                  <input
                    className="input text-ink-900"
                    maxLength={64}
                    value={String(meta.author ?? "")}
                    onChange={(e) =>
                      setMeta({ ...meta, author: e.target.value })
                    }
                  />
                </label>
                <label className="flex items-center gap-2 text-sm text-ink-700">
                  <input
                    type="checkbox"
                    checked={Boolean(meta.need_open_comment)}
                    onChange={(e) =>
                      setMeta({
                        ...meta,
                        need_open_comment: e.target.checked,
                      })
                    }
                  />
                  允许读者评论
                </label>
                <label className="flex items-center gap-2 text-sm text-ink-700">
                  <input
                    type="checkbox"
                    checked={Boolean(meta.only_fans_can_comment)}
                    onChange={(e) =>
                      setMeta({
                        ...meta,
                        only_fans_can_comment: e.target.checked,
                      })
                    }
                  />
                  仅关注者可评论
                </label>
              </>
            )}

            {platform === "toutiao" && (
              <label className="block space-y-1 text-sm text-ink-500">
                <span>作者（可选）</span>
                <input
                  className="input text-ink-900"
                  maxLength={64}
                  value={String(meta.author ?? "")}
                  onChange={(e) =>
                    setMeta({ ...meta, author: e.target.value })
                  }
                />
              </label>
            )}

            {spec.credential_fields.length === 0 && (
              <p className="text-xs text-ink-500">
                {spec.capability === "manual_export"
                  ? "该平台使用素材包导出，无需填写凭据。"
                  : "该平台接口待接入，可先保存账号。"}
              </p>
            )}

            {message && (
              <p
                className={
                  message.kind === "ok"
                    ? "text-sm text-brand-600"
                    : "text-sm text-coral-600"
                }
              >
                {message.text}
              </p>
            )}
            <button
              className="btn-primary w-full"
              disabled={create.isPending}
            >
              {create.isPending ? "保存中…" : "保存账号"}
            </button>
          </form>
        )}

        <section className="space-y-5">
          <h2 className="font-medium">已保存账号</h2>
          {accounts.isLoading && (
            <p className="text-sm text-ink-500">加载中…</p>
          )}
          {accounts.isError && (
            <p className="text-sm text-coral-600">
              {errorText(accounts.error)}
            </p>
          )}
          {accounts.isSuccess && groups.length === 0 && (
            <p className="card p-6 text-sm text-ink-500">
              还没有账号，先在左侧添加。
            </p>
          )}
          {groups.map((group) => (
            <div key={group.spec.key} className="space-y-2">
              <h3 className="flex items-center gap-2 text-sm text-ink-500">
                {group.spec.label}
                <span className="text-xs text-ink-500">
                  {group.items.length} 个
                </span>
              </h3>
              {group.items.map((account) => {
                const accountSpec =
                  specFor(account.platform, specs) ?? group.spec;
                const active = account.status === "active";
                const feedback =
                  rowFeedback?.id === account.id ? rowFeedback : null;
                return (
                  <article
                    key={account.id}
                    className="card p-4"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <h4 className="font-medium">{account.name}</h4>
                        <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-500">
                          <span>{group.spec.label}</span>
                          <span
                            className={`inline-flex rounded-full px-2 py-0.5 font-medium ${capabilityClass(account.capability)}`}
                          >
                            {CAPABILITY_LABELS[
                              account.capability as Capability
                            ] ?? account.capability}
                          </span>
                          <span>{account.capability_note}</span>
                        </p>
                      </div>
                      <span
                        className={
                          active
                            ? "text-xs text-brand-600"
                            : "text-xs text-ink-500"
                        }
                      >
                        {statusLabel(account.status)}
                      </span>
                    </div>
                    <p className="mt-2 text-xs text-ink-500">
                      凭据：
                      <span className="ml-1 font-mono">
                        {account.credential_masked === "{}"
                          ? "无需凭据或已销毁"
                          : account.credential_masked}
                      </span>
                    </p>

                    {active && isAdmin && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        <button
                          type="button"
                          className="rounded border border-line px-3 py-1.5 text-sm text-ink-700 hover:border-ink-300"
                          onClick={() => {
                            setEditing({
                              id: account.id,
                              name: account.name,
                            });
                            setCredentialFor("");
                            setRowFeedback(null);
                          }}
                        >
                          重命名
                        </button>
                        {accountSpec.credential_fields.length > 0 && (
                          <button
                            type="button"
                            className="rounded border border-line px-3 py-1.5 text-sm text-ink-700 hover:border-ink-300"
                            onClick={() => {
                              setCredentialFor(account.id);
                              setCredentialForm({});
                              setEditing(null);
                              setRowFeedback(null);
                            }}
                          >
                            更新凭据
                          </button>
                        )}
                        {revokingId === account.id ? (
                          <span className="flex flex-wrap items-center gap-2 rounded border border-coral-100 px-3 py-1.5 text-sm text-coral-600">
                            停用后凭据销毁且无法恢复
                            <button
                              type="button"
                              className="rounded bg-coral-600 px-2 py-1 text-xs font-medium text-white hover:bg-coral-700 disabled:opacity-50"
                              disabled={revoke.isPending}
                              onClick={() => revokeAccount(account)}
                            >
                              {revoke.isPending ? "停用中…" : "确认停用"}
                            </button>
                            <button
                              type="button"
                              className="rounded px-2 py-1 text-xs text-ink-500 hover:text-ink-900"
                              onClick={() => setRevokingId("")}
                            >
                              取消
                            </button>
                          </span>
                        ) : (
                          <button
                            type="button"
                            className="rounded border border-coral-100 px-3 py-1.5 text-sm text-coral-600 hover:border-coral-100 disabled:opacity-50"
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
                        className="mt-3 flex flex-wrap items-end gap-2 border-t border-line pt-3"
                        onSubmit={submitRename}
                      >
                        <label className="min-w-48 flex-1 space-y-1 text-sm text-ink-500">
                          <span>新名称</span>
                          <input
                            className="input text-ink-900"
                            maxLength={100}
                            required
                            value={editing.name}
                            onChange={(e) =>
                              setEditing({
                                ...editing,
                                name: e.target.value,
                              })
                            }
                          />
                        </label>
                        <button
                          className="btn-primary"
                          disabled={rename.isPending}
                        >
                          {rename.isPending ? "保存中…" : "保存"}
                        </button>
                        <button
                          type="button"
                          className="rounded border border-line px-4 py-2 text-sm text-ink-700"
                          onClick={() => setEditing(null)}
                        >
                          取消
                        </button>
                      </form>
                    )}

                    {credentialFor === account.id && (
                      <form
                        className="mt-3 space-y-2 border-t border-line pt-3"
                        onSubmit={(event) =>
                          submitCredential(event, accountSpec)
                        }
                      >
                        <p className="text-xs text-ink-500">
                          更新凭据需填写该平台的全部凭据字段。
                        </p>
                        <div className="flex flex-wrap items-end gap-2">
                          {accountSpec.credential_fields.map((field) => (
                            <label
                              key={field.key}
                              className="min-w-48 flex-1 space-y-1 text-sm text-ink-500"
                            >
                              <span>{field.label}</span>
                              <input
                                className="input text-ink-900"
                                type={field.secret ? "password" : "text"}
                                required={field.required}
                                value={credentialForm[field.key] ?? ""}
                                onChange={(e) =>
                                  setCredentialForm((prev) => ({
                                    ...prev,
                                    [field.key]: e.target.value,
                                  }))
                                }
                              />
                            </label>
                          ))}
                          <button
                            className="btn-primary"
                            disabled={updateCredential.isPending}
                          >
                            {updateCredential.isPending ? "保存中…" : "保存"}
                          </button>
                          <button
                            type="button"
                            className="rounded border border-line px-4 py-2 text-sm text-ink-700"
                            onClick={() => {
                              setCredentialFor("");
                              setCredentialForm({});
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
                            ? "mt-3 text-sm text-brand-600"
                            : "mt-3 text-sm text-coral-600"
                        }
                      >
                        {feedback.text}
                      </p>
                    )}
                  </article>
                );
              })}
            </div>
          ))}
        </section>
      </div>
    </div>
  );
}
