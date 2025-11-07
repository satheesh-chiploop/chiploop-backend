"use client";

import { useState, useEffect } from "react";

interface MissingAgentNamingDialogProps {
  missingAgents: string[];
  domain: "digital" | "analog" | "embedded" | "system";
  onCancel: () => void;
  onConfirm: (finalNames: string[]) => void;
}

export default function MissingAgentNamingDialog({
  missingAgents,
  domain,
  onCancel,
  onConfirm,
}: MissingAgentNamingDialogProps) {

  const [names, setNames] = useState<string[]>([]);

  // ✅ Deterministic S1 Naming
  useEffect(() => {
    const formatted = missingAgents.map((m) => {
      // normalize underscores → spaces
      let base = m.replace(/[_-]+/g, " ").trim();

      // title-case words
      base = base
        .split(" ")
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ");

      // add domain prefix
      const prefix = domain.charAt(0).toUpperCase() + domain.slice(1).toLowerCase();

      // ensure Agent suffix
      if (!base.toLowerCase().endsWith("agent")) {
        base = `${prefix} ${base} Agent`;
      } else {
        base = `${prefix} ${base}`;
      }

      return base;
    });

    setNames(formatted);
  }, [missingAgents, domain]);

  const updateName = (index: number, value: string) => {
    const copy = [...names];
    copy[index] = value;
    setNames(copy);
  };

  const handleConfirm = () => {
    const cleaned = names.map((n) => {
      let v = n.trim();

      if (!v.toLowerCase().endsWith("agent")) {
        v = `${v} Agent`;
      }

      return v.replace(/\s+/g, " ").trim();
    });

    onConfirm(cleaned);
  };

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-slate-800 border border-cyan-400 rounded-xl p-6 w-[540px] shadow-lg">

        <h2 className="text-xl font-semibold text-cyan-300 mb-4">
          🧱 Name Your New Agents
        </h2>

        <p className="text-sm text-slate-300 mb-4">
          These will be added to your <b>Custom Agents</b> library. You can rename them below.
        </p>

        <div className="max-h-64 overflow-y-auto space-y-3 pr-1">
          {names.map((value, idx) => (
            <div key={idx} className="flex items-center gap-2">
              <div className="min-w-[140px] text-xs text-slate-400">
                {missingAgents[idx]}
              </div>

              <input
                value={value}
                onChange={(e) => updateName(idx, e.target.value)}
                className="flex-1 px-2 py-1 bg-slate-900 border border-cyan-500 rounded text-white text-sm focus:outline-none focus:ring-cyan-400"
              />
            </div>
          ))}
        </div>

        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm rounded bg-slate-700 hover:bg-slate-600 text-white"
          >
            Cancel
          </button>

          <button
            onClick={handleConfirm}
            className="px-4 py-2 text-sm rounded bg-cyan-400 hover:bg-cyan-300 text-black font-semibold"
          >
            Generate Agents
          </button>
        </div>

      </div>
    </div>
  );
}
