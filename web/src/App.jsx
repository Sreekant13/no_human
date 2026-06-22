import { useEffect, useReducer, useRef, useState } from "react";
// (clock removed — the operator board doesn't need a wall clock)
import { connectWS, fetchTasks } from "./api.js";
import Board from "./Board.jsx";

function tasksReducer(state, action) {
  switch (action.type) {
    case "set":
      return action.tasks;
    case "sync": {
      const map = Object.fromEntries(state.map((t) => [t.id, t]));
      action.tasks.forEach((t) => { map[t.id] = t; });
      return Object.values(map);
    }
    default:
      return state;
  }
}

export default function App() {
  const [tasks, dispatch] = useReducer(tasksReducer, []);
  const [wsLive, setWsLive] = useState(false);
  const [fetchError, setFetchError] = useState(null);
  const wsRef = useRef(null);

  // initial load
  useEffect(() => {
    fetchTasks()
      .then((ts) => { setFetchError(null); dispatch({ type: "set", tasks: ts }); })
      .catch((err) => setFetchError(err?.message || "Cannot reach the no_human API."));
  }, []);

  // WebSocket
  useEffect(() => {
    function connect() {
      const ws = connectWS((msg) => {
        if (msg.tasks) dispatch({ type: "sync", tasks: msg.tasks });
      });
      ws.onopen = () => setWsLive(true);
      ws.onclose = () => {
        setWsLive(false);
        setTimeout(connect, 3000);
      };
      wsRef.current = ws;
    }
    connect();
    return () => wsRef.current?.close();
  }, []);

  if (fetchError) {
    return (
      <div className="nh-shell">
        <header className="nh-header">
          <div className="nh-logo">no_human<span> // operator terminal</span></div>
        </header>
        <div className="nh-center">
          <div className="nh-error">
            <div>API unavailable: {fetchError}</div>
            <button
              className="btn btn-sendback"
              style={{ marginTop: 12 }}
              onClick={() => window.location.reload()}
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="nh-shell">
      <header className="nh-header">
        <div className="nh-logo">
          no_human<span> // operator terminal</span>
        </div>
        <div className="nh-header-right">
          <div className={`nh-ws-dot${wsLive ? " live" : ""}`} title={wsLive ? "live" : "reconnecting"} />
        </div>
      </header>
      <Board tasks={tasks} />
    </div>
  );
}
