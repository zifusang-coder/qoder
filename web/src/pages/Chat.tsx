import { Link } from "react-router-dom";

export default function Chat() {
  return (
    <main className="mx-auto flex h-screen max-w-3xl flex-col p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">运营助手</h1>
        <Link className="text-sm text-indigo-400" to="/settings">
          设置
        </Link>
      </div>
      <p className="mt-2 text-sm text-amber-400">对话编排将在 Phase 1 接入，当前为界面骨架。</p>
      <div
        className="mt-4 flex-1 space-y-3 overflow-y-auto rounded-lg border border-slate-800 bg-slate-900 p-4 text-sm text-slate-500"
        role="status"
      >
        对话能力将在 Phase 1 接入
      </div>
      <div className="mt-4 flex gap-2">
        <input
          className="flex-1 rounded border border-slate-700 bg-slate-900 px-3 py-2 disabled:opacity-50"
          disabled
          placeholder="对话能力将在 Phase 1 接入"
        />
        <button className="rounded bg-indigo-600 px-4 py-2 disabled:opacity-50" disabled>
          发送
        </button>
      </div>
    </main>
  );
}
