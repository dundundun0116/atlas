import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import AtlasPage from "./AtlasPage";
import "./styles.css";

const isAtlas = window.location.pathname.startsWith("/atlas");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {isAtlas ? <AtlasPage /> : <App />}
  </React.StrictMode>,
);
