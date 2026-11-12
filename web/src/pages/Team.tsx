import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import type { Role } from "../auth";
import { useAuth } from "../auth-context";
import { api } from "../client";
import {
  canEditMember,
  canManageMembers,
  memberStatusLabel,
  roleLabel,
} from "../team";
import { errorText, type Member, type MemberCreated } from "../types";

const ROLE_VALUES: Role[] = ["viewer", "editor", "admin", "owner"];

export default function Team() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [form, setForm] = useState({
    email: "",
    display_name: "",
    role: "editor" as Role,
  });
  const [created, setCreated] = useState<MemberCreated | null>(null);
  const [message, setMessage] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);

  const members = useQuery({
    queryKey: ["members"],
    queryFn: () => api<Member[]>("/members"),
  });

  const create = useMutation({
    mutationFn: (payload: {
      email: string;
      display_name: string;
      role: Role;
    }) =>
      api<MemberCreated>("/members", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (member) => {
      setCreated(member);
      setForm({ email: "", display_name: "", role: "editor" });
      setMessage({ kind: "ok", text: `已创建成员 ${member.email}` });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: () => setCreated(null),
  });

  const update = useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: { role?: Role; is_active?: boolean };
    }) =>
      api<Member>(`/members/${id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      setMessage({ kind: "ok", text: "成员已更新" });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (err) => setMessage({ kind: "error", text: errorText(err) }),
  });

  const disable = useMutation({
    mutationFn: (id: string) =>
      api<void>(`/members/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setMessage({ kind: "ok", text: "成员已停用" });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (err) => setMessage({ kind: "error", text: errorText(err) }),
  });

  const canManage = user ? canManageMembers(user.role) : false;

  function submit(event: FormEvent) {
    event.preventDefault();
    const email = form.email.trim().toLowerCase();
    const displayName = form.display_name.trim();
    if (!email || !displayName) {
      setMessage({ kind: "error", text: "请填写邮箱与显示名" });
      return;
    }
    setCreated(null);
    setMessage(null);
    create.mutate({ email, display_name: displayName, role: form.role });
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">团队</h1>
        <p className="mt-1 text-sm text-slate-400">
          管理当前租户的成员与角色。管理员可查看，所有者可创建与调整。
        </p>
      </div>

      {!canManage && (
        <p className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
          只有租户所有者可以创建成员或调整角色。
        </p>
      )}

      {canManage && (
        <form
          className="grid gap-3 rounded-xl border border-slate-800 bg-slate-900 p-5 md:grid-cols-[1fr_1fr_140px_auto]"
          onSubmit={submit}
        >
          <label className="space-y-1 text-sm text-slate-400">
            <span>邮箱 *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={255}
              required
              type="email"
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
              placeholder="member@example.com"
            />
          </label>
          <label className="space-y-1 text-sm text-slate-400">
            <span>显示名 *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={100}
              required
              value={form.display_name}
              onChange={(e) =>
                setForm({ ...form, display_name: e.target.value })
              }
              placeholder="例如：运营小林"
            />
          </label>
          <label className="space-y-1 text-sm text-slate-400">
            <span>角色</span>
            <select
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              value={form.role}
              onChange={(e) =>
                setForm({ ...form, role: e.target.value as Role })
              }
            >
              {ROLE_VALUES.map((role) => (
                <option key={role} value={role}>
                  {roleLabel(role)}
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-end">
            <button
              className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50 md:w-auto"
              disabled={create.isPending}
            >
              {create.isPending ? "创建中…" : "创建成员"}
            </button>
          </div>
        </form>
      )}

      {created && (
        <div className="rounded-xl border border-emerald-900 bg-slate-900 p-4 text-sm">
          <p className="text-emerald-400">
            已创建 {created.email}，请立即把一次性临时密码交给成员：
          </p>
          <code className="mt-2 block break-all rounded bg-slate-950 px-3 py-2 font-mono text-slate-100">
            {created.initial_password}
          </code>
          <p className="mt-2 text-xs text-slate-500">
            临时密码只显示一次；成员首次登录后必须修改密码。
          </p>
        </div>
      )}

      {message && (
        <p
          className={
            message.kind === "error"
              ? "text-sm text-red-400"
              : "text-sm text-emerald-400"
          }
        >
          {message.text}
        </p>
      )}
      {create.isError && (
        <p className="text-sm text-red-400">{errorText(create.error)}</p>
      )}

      <section className="space-y-2">
        <h2 className="font-medium">成员列表</h2>
        {members.isLoading && (
          <p className="text-sm text-slate-400">加载中…</p>
        )}
        {members.isError && (
          <p className="text-sm text-red-400">{errorText(members.error)}</p>
        )}
        {members.data?.length === 0 && (
          <p className="text-sm text-slate-400">还没有其他成员。</p>
        )}
        {members.data?.map((member) => {
          const isSelf = member.id === user?.id;
          const editable = user ? canEditMember(user, member) : false;
          const pending =
            (update.isPending && update.variables?.id === member.id) ||
            (disable.isPending && disable.variables === member.id);
          return (
            <article
              key={member.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 p-4"
            >
              <div>
                <h3 className="font-medium">
                  {member.display_name}
                  {isSelf && (
                    <span className="ml-2 text-xs text-slate-500">（我）</span>
                  )}
                </h3>
                <p className="mt-1 text-xs text-slate-500">
                  {member.email} · 加入于 {member.created_at.slice(0, 10)}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span
                  className={
                    member.is_active
                      ? "text-xs text-emerald-400"
                      : "text-xs text-slate-500"
                  }
                >
                  {memberStatusLabel(member.is_active)}
                </span>
                <select
                  aria-label={`角色 ${member.email}`}
                  className="rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100 disabled:opacity-50"
                  value={member.role}
                  disabled={!editable || pending}
                  onChange={(e) =>
                    update.mutate({
                      id: member.id,
                      payload: { role: e.target.value as Role },
                    })
                  }
                >
                  {ROLE_VALUES.map((role) => (
                    <option key={role} value={role}>
                      {roleLabel(role)}
                    </option>
                  ))}
                </select>
                {member.is_active ? (
                  <button
                    type="button"
                    className="rounded border border-red-900 px-3 py-1.5 text-red-400 hover:border-red-800 disabled:opacity-50"
                    disabled={!editable || pending}
                    onClick={() => disable.mutate(member.id)}
                  >
                    停用
                  </button>
                ) : (
                  <button
                    type="button"
                    className="rounded border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-600 disabled:opacity-50"
                    disabled={!editable || pending}
                    onClick={() =>
                      update.mutate({
                        id: member.id,
                        payload: { is_active: true },
                      })
                    }
                  >
                    启用
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
}
