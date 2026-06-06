// dashboard/src/components/ResearchTab.jsx
//
// v2.20.0 Research tab composer — two structured sub-panes:
//   DATASET   — read-only canonical OHLC store inspector (Mode A read)
//   LAB       — Mode B hypothesis lab
//
// Owns only the local sub-tab state. All data + ws comes from App.jsx as props.
// v2.20.1: restyled to use shared theme tokens (COLORS, mono) — was a generic
// dark-template aesthetic before.

import React, { useState } from "react";
import DatasetPane from "./research/DatasetPane";
import LabPane from "./research/LabPane";
import { COLORS, mono } from "../theme";

const TABS = [
  ["DATASET", "Dataset"],
  ["LAB", "Lab"],
];

export default function ResearchTab({
  sendMessage,
  coverageData,
  labResult,
  labProgress,
  paramsSchema,
  clearLabRunState,
}) {
  const [pane, setPane] = useState("DATASET");

  return (
    <div>
      <nav
        style={{
          borderBottom: `1px solid ${COLORS.panelBorder}`,
          padding: "0 24px",
          display: "flex",
          gap: 4,
        }}
      >
        {TABS.map(([id, label]) => {
          const active = pane === id;
          return (
            <button
              key={id}
              onClick={() => setPane(id)}
              style={{
                background: "transparent",
                border: "none",
                color: active ? COLORS.text : COLORS.textDim,
                padding: "14px 18px",
                cursor: "pointer",
                borderBottom: active
                  ? `2px solid ${COLORS.accent}`
                  : "2px solid transparent",
                fontFamily: mono,
                fontSize: 11,
                fontWeight: active ? 700 : 500,
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                outline: "none",
                transition: "color 160ms, border-color 160ms",
              }}
            >
              {label}
            </button>
          );
        })}
      </nav>

      {pane === "DATASET" && (
        <DatasetPane sendMessage={sendMessage} coverageData={coverageData} />
      )}
      {pane === "LAB" && (
        <LabPane
          sendMessage={sendMessage}
          labResult={labResult}
          paramsSchema={paramsSchema}
          labProgress={labProgress}
          clearLabRunState={clearLabRunState}
        />
      )}
    </div>
  );
}
