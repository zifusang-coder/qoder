import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api } from "../client";
import {
  errorText,
  type Profile,
  type ProfileList,
  type ProfileMemory,
} from "../types";

type ProfilePayload = Omit<Profile, "id" | "is_active">;

const EMPTY_FORM = {
  name: "",
  account_name: "",
  positioning: "",
  audience: "",
  tone: "",
  taboos: "",
  goals: "",
  platform_focus: "",
};

function splitList(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ].slice(0, 20);
}

function sameList(raw: string, values: string[]): boolean {
  const current = splitList(raw);
  return (
    current.length === values.length &&
    current.every((value, index) => value === values[index])
  );
}

function formMatchesPayload(
  form: typeof EMPTY_FORM,
  payload: ProfilePayload,
): boolean {
  return (
    form.name === payload.name &&
    form.account_name === payload.account_name &&
    form.positioning === payload.positioning &&
    form.audience === payload.audience &&
    form.tone === payload.tone &&
    sameList(form.taboos, payload.taboos) &&
    sameList(form.goals, payload.goals) &&
    sameList(form.platform_focus, payload.platform_focus)
  );
}

export default function Profiles() {
  const qc = useQueryClient();
  const [form, setForm] = useState(EMPTY_FORM);
  const [formMessage, setFormMessage] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [memoryContent, setMemoryContent] = useState("");
  const [memoryKind, setMemoryKind] = useState("insight");
  const [memoryWeight, setMemoryWeight] = useState("1");
  const [memoryMessage, setMemoryMessage] = useState("");

  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api<ProfileList>("/profiles"),
  });

  const items = profiles.data?.items ?? [];
  const selected =
    items.find((profile) => profile.id === selectedId) ?? items[0];
  const profileId = selected?.id ?? "";

  const memories = useQuery({
    queryKey: ["memories", profileId],
    queryFn: () => api<ProfileMemory[]>(`/profiles/${profileId}/memories`),
    enabled: Boolean(profileId),
  });

  const createProfile = useMutation({
    mutationFn: (payload: ProfilePayload) =>
      api<Profile>("/profiles", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (profile, payload) => {
      // Insert the new profile optimistically so `selected` resolves before
      // the list refetch finishes.
      qc.setQueryData<ProfileList>(["profiles"], (old) =>
        old
          ? {
              ...old,
              items: [profile, ...old.items.filter((p) => p.id !== profile.id)],
            }
          : { items: [profile] },
      );
      qc.invalidateQueries({ queryKey: ["profiles"] });
      setSelectedId(profile.id);
      setForm((prev) => (formMatchesPayload(prev, payload) ? EMPTY_FORM : prev));
      setFormMessage("");
    },
  });

  const addMemory = useMutation({
    mutationFn: (payload: {
      profileId: string;
      content: string;
      kind: string;
      weight: number;
    }) =>
      api<ProfileMemory>(`/profiles/${payload.profileId}/memories`, {
        method: "POST",
        body: JSON.stringify({
          content: payload.content,
          kind: payload.kind,
          weight: payload.weight,
        }),
      }),
    onSuccess: (_memory, vars) => {
      qc.invalidateQueries({ queryKey: ["memories", vars.profileId] });
      setMemoryContent((prev) => (prev === vars.content ? "" : prev));
    },
  });

  function submitProfile(event: FormEvent) {
    event.preventDefault();
    setFormMessage("");
    const name = form.name.trim();
    if (!name) {
      setFormMessage("请填写画像名称");
      return;
    }
    createProfile.mutate({
      name,
      account_name: form.account_name.trim(),
      positioning: form.positioning.trim(),
      audience: form.audience.trim(),
      tone: form.tone.trim(),
      taboos: splitList(form.taboos),
      goals: splitList(form.goals),
      platform_focus: splitList(form.platform_focus),
    });
  }

  function submitMemory(event: FormEvent) {
    event.preventDefault();
    setMemoryMessage("");
    const content = memoryContent.trim();
    if (!content) {
      setMemoryMessage("请输入记忆内容");
      return;
    }
    const weight = Number(memoryWeight);
    if (!Number.isFinite(weight) || weight < 0 || weight > 100) {
      setMemoryMessage("权重需在 0–100 之间");
      return;
    }
    addMemory.mutate({
      profileId,
      content,
      kind: memoryKind.trim() || "insight",
      weight,
    });
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">账号画像</h1>
        <p className="mt-1 text-sm text-slate-400">
          维护账号定位与长期记忆，创作技能会自动注入这些信息。
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[340px_1fr]">
        <div className="space-y-6">
          <form
            className="space-y-3 rounded-xl border border-slate-800 bg-slate-900 p-5"
            onSubmit={submitProfile}
          >
            <h2 className="font-medium">新建画像</h2>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>名称 *</span>
              <input
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                maxLength={100}
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="例如：产品经理小林"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>账号名</span>
              <input
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                maxLength={100}
                value={form.account_name}
                onChange={(e) =>
                  setForm({ ...form, account_name: e.target.value })
                }
                placeholder="平台昵称"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>定位</span>
              <textarea
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                rows={2}
                maxLength={5000}
                value={form.positioning}
                onChange={(e) =>
                  setForm({ ...form, positioning: e.target.value })
                }
                placeholder="账号提供什么价值"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>受众</span>
              <textarea
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                rows={2}
                maxLength={5000}
                value={form.audience}
                onChange={(e) =>
                  setForm({ ...form, audience: e.target.value })
                }
                placeholder="目标读者是谁"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>语气</span>
              <textarea
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                rows={2}
                maxLength={5000}
                value={form.tone}
                onChange={(e) => setForm({ ...form, tone: e.target.value })}
                placeholder="例如：专业、克制、有案例"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>禁区（逗号分隔）</span>
              <input
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                value={form.taboos}
                onChange={(e) => setForm({ ...form, taboos: e.target.value })}
                placeholder="绝对化用语，竞品名"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>目标（逗号分隔）</span>
              <input
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                value={form.goals}
                onChange={(e) => setForm({ ...form, goals: e.target.value })}
                placeholder="涨粉，建立专业形象"
              />
            </label>
            <label className="block space-y-1 text-sm text-slate-400">
              <span>重点平台（逗号分隔）</span>
              <input
                className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                value={form.platform_focus}
                onChange={(e) =>
                  setForm({ ...form, platform_focus: e.target.value })
                }
                placeholder="公众号，小红书"
              />
            </label>
            {(formMessage || createProfile.isError) && (
              <p className="text-sm text-red-400">
                {formMessage || errorText(createProfile.error)}
              </p>
            )}
            <button
              className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
              disabled={createProfile.isPending}
            >
              {createProfile.isPending ? "创建中…" : "创建画像"}
            </button>
          </form>

          <section className="space-y-2">
            <h2 className="font-medium">画像列表</h2>
            {profiles.isLoading && (
              <p className="text-sm text-slate-400">加载中…</p>
            )}
            {profiles.isError && (
              <p className="text-sm text-red-400">
                {errorText(profiles.error)}
              </p>
            )}
            {profiles.isSuccess && items.length === 0 && (
              <p className="text-sm text-slate-400">还没有画像，先创建一个。</p>
            )}
            {items.map((profile) => {
              const active = profile.id === selected?.id;
              return (
                <button
                  key={profile.id}
                  type="button"
                  className={`w-full rounded border p-3 text-left text-sm ${
                    active
                      ? "border-indigo-500 bg-slate-800"
                      : "border-slate-800 bg-slate-900 hover:border-slate-700"
                  }`}
                  onClick={() => {
                    if (profile.id === selected?.id) return;
                    setSelectedId(profile.id);
                    setMemoryContent("");
                    setMemoryMessage("");
                    addMemory.reset();
                  }}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="font-medium">{profile.name}</span>
                    {!profile.is_active && (
                      <span className="text-xs text-slate-500">已停用</span>
                    )}
                  </span>
                  <span className="mt-1 block text-xs text-slate-400">
                    {profile.account_name || "未填写账号名"}
                  </span>
                </button>
              );
            })}
          </section>
        </div>

        <div className="space-y-6">
          {selected ? (
            <>
              <section className="space-y-4 rounded-xl border border-slate-800 bg-slate-900 p-6">
                <div className="flex items-center justify-between gap-3">
                  <h2 className="font-medium">{selected.name}</h2>
                  <span className="text-xs text-slate-500">
                    {selected.account_name || "未填写账号名"}
                  </span>
                </div>
                <dl className="grid gap-4 text-sm sm:grid-cols-2">
                  <Info label="定位" value={selected.positioning} />
                  <Info label="受众" value={selected.audience} />
                  <Info label="语气" value={selected.tone} />
                </dl>
                <Chips label="禁区" values={selected.taboos} />
                <Chips label="目标" values={selected.goals} />
                <Chips
                  label="重点平台"
                  values={selected.platform_focus}
                />
              </section>

              <section className="space-y-3 rounded-xl border border-slate-800 bg-slate-900 p-6">
                <h2 className="font-medium">画像记忆</h2>
                <form
                  className="flex flex-wrap items-end gap-2"
                  onSubmit={submitMemory}
                >
                  <label className="min-w-40 flex-1 space-y-1 text-sm text-slate-400">
                    <span>新增记忆</span>
                    <input
                      className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                      maxLength={5000}
                      value={memoryContent}
                      onChange={(e) => setMemoryContent(e.target.value)}
                      placeholder="例如：读者更喜欢带数据的长文"
                    />
                  </label>
                  <label className="space-y-1 text-sm text-slate-400">
                    <span>类型</span>
                    <input
                      className="w-28 rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                      maxLength={32}
                      value={memoryKind}
                      onChange={(e) => setMemoryKind(e.target.value)}
                    />
                  </label>
                  <label className="space-y-1 text-sm text-slate-400">
                    <span>权重</span>
                    <input
                      className="w-24 rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
                      type="number"
                      min={0}
                      max={100}
                      step={1}
                      value={memoryWeight}
                      onChange={(e) => setMemoryWeight(e.target.value)}
                    />
                  </label>
                  <button
                    className="rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
                    disabled={addMemory.isPending}
                  >
                    {addMemory.isPending ? "添加中…" : "添加"}
                  </button>
                </form>
                {(memoryMessage || addMemory.isError) && (
                  <p className="text-sm text-red-400">
                    {memoryMessage || errorText(addMemory.error)}
                  </p>
                )}
                {memories.isLoading && (
                  <p className="text-sm text-slate-400">加载中…</p>
                )}
                {memories.isError && (
                  <p className="text-sm text-red-400">
                    {errorText(memories.error)}
                  </p>
                )}
                {memories.isSuccess && memories.data.length === 0 && (
                  <p className="text-sm text-slate-400">
                    还没有记忆，添加一条试试。
                  </p>
                )}
                <ul className="space-y-2">
                  {memories.data?.map((memory) => (
                    <li
                      key={memory.id}
                      className="rounded border border-slate-800 bg-slate-950 p-3 text-sm"
                    >
                      <p className="whitespace-pre-wrap">{memory.content}</p>
                      <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-500">
                        <span>类型：{memory.kind}</span>
                        <span>权重：{memory.weight}</span>
                        <span>来源：{memory.source}</span>
                        {memory.tags.length > 0 && (
                          <span>标签：{memory.tags.join("、")}</span>
                        )}
                      </p>
                    </li>
                  ))}
                </ul>
              </section>
            </>
          ) : (
            <p className="rounded-xl border border-slate-800 bg-slate-900 p-6 text-sm text-slate-400">
              选择左侧画像查看详情与记忆。
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-1 whitespace-pre-wrap text-slate-200">
        {value || "—"}
      </dd>
    </div>
  );
}

function Chips({ label, values }: { label: string; values: string[] }) {
  if (values.length === 0) return null;
  return (
    <div className="text-sm">
      <p className="text-xs text-slate-500">{label}</p>
      <div className="mt-1 flex flex-wrap gap-1">
        {values.map((value, index) => (
          <span
            key={`${value}-${index}`}
            className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-300"
          >
            {value}
          </span>
        ))}
      </div>
    </div>
  );
}
