import { useQuery } from "@tanstack/react-query";
import {
  FormEvent,
  KeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { Link } from "react-router-dom";
import { api } from "../client";
import { errorText, type ChatOut, type ProfileList } from "../types";

type Turn = {
  id: number;
  role: "user" | "assistant";
  text: string;
  meta?: ChatOut;
};

function AssistantMeta({ meta }: { meta: ChatOut }) {
  return (
    <div className="mt-1 space-y-1 text-xs text-ink-500">
      <p>
        {meta.intent === "skill"
          ? `技能：${meta.skill_id ?? "未知"}`
          : "通用问答"}
        {meta.run_id && ` · 运行 ${meta.run_id.slice(0, 8)}`}
      </p>
      {meta.missing && meta.missing.length > 0 && (
        <p className="text-warm-600">还需要：{meta.missing.join("、")}</p>
      )}
      {meta.data && (
        <details>
          <summary className="cursor-pointer text-sage-700">
            查看结构化结果
          </summary>
          <pre className="mt-1 overflow-x-auto rounded border border-line bg-white p-2 text-ink-700">
            {JSON.stringify(meta.data, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

export default function Chat() {
  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api<ProfileList>("/profiles"),
  });
  const [profileId, setProfileId] = useState("");
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const nextId = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns, sending]);

  async function send() {
    const text = message.trim();
    if (!text || sending) return;
    if (text.length > 4000) {
      setError("消息不能超过 4000 字");
      return;
    }
    setError("");
    setMessage("");
    setTurns((prev) => [
      ...prev,
      { id: nextId.current++, role: "user", text },
    ]);
    setSending(true);
    try {
      const result = await api<ChatOut>("/chat", {
        method: "POST",
        body: JSON.stringify({
          message: text,
          ...(profileId ? { profile_id: profileId } : {}),
        }),
      });
      setTurns((prev) => [
        ...prev,
        {
          id: nextId.current++,
          role: "assistant",
          text: result.reply,
          meta: result,
        },
      ]);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSending(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // 中文输入法用 Enter 选词时不能发送
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      void send();
    }
  }

  return (
    <section className="mx-auto flex h-[calc(100vh-9rem)] w-full max-w-3xl flex-col">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold">运营助手</h1>
          <p className="mt-1 text-xs text-ink-500">
            关键词会路由到技能；未命中时使用通用问答。
          </p>
        </div>
        <Link className="text-sm text-sage-700" to="/settings">
          LLM 设置
        </Link>
      </div>

      <label className="mt-3 flex flex-wrap items-center gap-2 text-xs text-ink-500">
        <span>画像</span>
        <select
          className="rounded border border-line bg-surface px-2 py-1 text-ink-700 disabled:opacity-50"
          value={profileId}
          onChange={(e) => setProfileId(e.target.value)}
          disabled={!profiles.data}
        >
          <option value="">不注入画像</option>
          {(profiles.data?.items ?? [])
            .filter((profile) => profile.is_active)
            .map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.name}
              </option>
            ))}
        </select>
        {profiles.isError && (
          <span className="text-wine-600">{errorText(profiles.error)}</span>
        )}
      </label>

      <div className="mt-4 flex-1 space-y-3 overflow-y-auto rounded-lg border border-line bg-surface p-4 text-sm">
        {turns.length === 0 && !sending && (
          <div className="space-y-1 text-ink-500">
            <p>可以直接描述目标，例如「帮我想 5 个公众号选题」。</p>
            <p>技能缺少参数时会提示还需要补充哪些字段。</p>
          </div>
        )}
        {turns.map((turn) => (
          <div
            key={turn.id}
            className={turn.role === "user" ? "text-right" : "text-left"}
          >
            <div
              className={`inline-block max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 ${
                turn.role === "user"
                  ? "bg-sage-600 text-white"
                  : "bg-sage-50 text-ink-900"
              }`}
            >
              {turn.text}
            </div>
            {turn.meta && <AssistantMeta meta={turn.meta} />}
          </div>
        ))}
        {sending && <p className="text-ink-500">生成中…</p>}
        <div ref={endRef} />
      </div>

      {error && <p className="mt-2 text-sm text-wine-600">{error}</p>}

      <form className="mt-3 flex items-end gap-2" onSubmit={submit}>
        <textarea
          className="min-h-16 flex-1 resize-none rounded border border-line bg-surface px-3 py-2 text-sm disabled:opacity-50"
          maxLength={4000}
          rows={2}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={sending}
          placeholder="描述你的目标；Enter 发送，Shift+Enter 换行"
          aria-label="对话输入"
        />
        <button
          className="btn-primary"
          disabled={sending || !message.trim()}
        >
          {sending ? "生成中…" : "发送"}
        </button>
      </form>
    </section>
  );
}
