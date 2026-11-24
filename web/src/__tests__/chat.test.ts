import { describe, expect, it } from "vitest";
import { conversationTitle, messageMeta, turnsFromDetail } from "../chat";
import type { ChatConversationDetail } from "../types";

const detail: ChatConversationDetail = {
  id: "c1",
  title: "你好",
  profile_id: null,
  updated_at: "2026-11-22T10:00:00Z",
  messages: [
    {
      id: "m1",
      role: "user",
      content: "你好",
      intent: null,
      skill_id: null,
      run_id: null,
      data: null,
      missing: null,
      created_at: "2026-11-22T10:00:00Z",
    },
    {
      id: "m2",
      role: "assistant",
      content: "这是运营建议",
      intent: "chat",
      skill_id: null,
      run_id: null,
      data: null,
      missing: null,
      created_at: "2026-11-22T10:00:01Z",
    },
    {
      id: "m3",
      role: "assistant",
      content: "还需要：title",
      intent: "skill",
      skill_id: "postmortem",
      run_id: null,
      data: null,
      missing: ["title"],
      created_at: "2026-11-22T10:00:02Z",
    },
  ],
};

describe("chat helpers", () => {
  it("turnsFromDetail keeps order and attaches meta to assistant replies", () => {
    const turns = turnsFromDetail(detail);
    expect(turns.map((turn) => turn.id)).toEqual(["m1", "m2", "m3"]);
    expect(turns[0].meta).toBeUndefined();
    expect(turns[1].meta).toMatchObject({
      reply: "这是运营建议",
      intent: "chat",
    });
    expect(turns[2].meta?.missing).toEqual(["title"]);
    expect(turns[2].meta?.skill_id).toBe("postmortem");
  });

  it("messageMeta only applies to assistant messages with an intent", () => {
    expect(messageMeta(detail.messages[0])).toBeNull();
    expect(
      messageMeta({ ...detail.messages[1], intent: null }),
    ).toBeNull();
  });

  it("conversationTitle falls back for blank titles", () => {
    expect(conversationTitle("  周报选题  ")).toBe("周报选题");
    expect(conversationTitle("   ")).toBe("新对话");
  });
});
