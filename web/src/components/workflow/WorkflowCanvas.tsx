import "@xyflow/react/dist/style.css";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import { useEffect, useMemo } from "react";
import type { Graph } from "../../types/workflow";
import WorkflowNodeView from "./WorkflowNode";

const nodeTypes = {
  input: WorkflowNodeView,
  tool: WorkflowNodeView,
  agent: WorkflowNodeView,
  condition: WorkflowNodeView,
  review: WorkflowNodeView,
  output: WorkflowNodeView,
};

function fallbackPosition(index: number): { x: number; y: number } {
  return { x: 80 + (index % 4) * 240, y: 80 + Math.floor(index / 4) * 150 };
}

function toFlowNodes(
  graph: Graph,
  statuses: Record<string, string>,
  selectedId: string | null,
): Node[] {
  return graph.nodes.map((node, index) => {
    const position =
      node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y)
        ? node.position
        : fallbackPosition(index);
    return {
      id: node.id,
      type: node.type,
      position,
      selected: node.id === selectedId,
      data: {
        ...node.data,
        kind: node.type,
        status: statuses[node.id] ?? "",
      },
    };
  });
}

function toFlowEdges(graph: Graph, executed: Set<string>): Edge[] {
  return graph.edges.map((edge) => {
    const active = executed.has(edge.id);
    const label =
      edge.sourceHandle === "true" ? "是" : edge.sourceHandle === "false" ? "否" : undefined;
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle ?? undefined,
      label,
      animated: active,
      style: active ? { strokeWidth: 2.5, stroke: "#3f8a66" } : undefined,
      labelStyle: { fontSize: 10, fill: "#6b7570" },
    };
  });
}

export default function WorkflowCanvas({
  graph,
  version,
  readOnly = false,
  selectedId = null,
  statuses = {},
  executedEdges,
  onSelect,
  onMoveNode,
  onConnectEdge,
  onDeleteNodes,
  onDeleteEdges,
}: {
  graph: Graph;
  version: number;
  readOnly?: boolean;
  selectedId?: string | null;
  statuses?: Record<string, string>;
  executedEdges?: Set<string>;
  onSelect?: (id: string | null) => void;
  onMoveNode?: (id: string, position: { x: number; y: number }) => void;
  onConnectEdge?: (connection: Connection) => void;
  onDeleteNodes?: (ids: string[]) => void;
  onDeleteEdges?: (ids: string[]) => void;
}) {
  const emptyExecuted = useMemo(() => new Set<string>(), []);
  const [nodes, setNodes, onNodesChange] = useNodesState(
    toFlowNodes(graph, statuses, selectedId),
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState(
    toFlowEdges(graph, executedEdges ?? emptyExecuted),
  );

  useEffect(() => {
    setNodes(toFlowNodes(graph, statuses, selectedId));
    setEdges(toFlowEdges(graph, executedEdges ?? emptyExecuted));
    // version 是页面提交图变更的序号；selectedId 单独变化时只更新选中态。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  useEffect(() => {
    setNodes((current) =>
      current.map((node) => ({ ...node, selected: node.id === selectedId })),
    );
  }, [selectedId, setNodes]);

  return (
    <div className="h-[520px] overflow-hidden rounded-xl border border-line bg-surface">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, node) => onSelect?.(node.id)}
        onPaneClick={() => onSelect?.(null)}
        onNodeDragStop={(_, node) => onMoveNode?.(node.id, node.position)}
        onConnect={(connection) => onConnectEdge?.(connection)}
        onNodesDelete={(deleted) => onDeleteNodes?.(deleted.map((node) => node.id))}
        onEdgesDelete={(deleted) => onDeleteEdges?.(deleted.map((edge) => edge.id))}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
        deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
        snapToGrid
        snapGrid={[16, 16]}
        defaultEdgeOptions={{ type: "smoothstep" }}
        fitView
        minZoom={0.3}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#e3e5e1" />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => {
            const status = String((node.data as Record<string, unknown>).status ?? "");
            if (status === "running") return "#3f8a66";
            if (status === "error" || status === "denied") return "#8c3a4a";
            if (status === "skipped") return "#e3e5e1";
            return "#dcebe1";
          }}
        />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
