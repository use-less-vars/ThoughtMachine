import React, { useState, useEffect } from 'react';

const ConfigPanel = ({ config, sendCommand }) => {
  const [draft, setDraft] = useState({ ...config });

  useEffect(() => {
    setDraft({ ...config });
  }, [config]);

  return (
    <div style={{ padding: '1rem', fontFamily: 'monospace', whiteSpace: 'pre-wrap', background: '#f5f5f5' }}>
      <h3>Config (read‑only)</h3>
      {JSON.stringify(draft, null, 2)}
      <div style={{ marginTop: '0.5rem' }}>
        <button onClick={() => console.log(draft)}>Log Draft (console)</button>
      </div>
    </div>
  );
};

export default ConfigPanel;
