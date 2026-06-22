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
  const wsRef = useRef(null);

  // initial load
  useEffect(() => {
    fetchTasks()
      .then((ts) => dispatch({ type: "set", tasks: ts }))
      .catch(() => {});
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
