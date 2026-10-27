(ns logseq.db-sync.proxy-origin-test
  (:require [logseq.db-sync.common :as common]))

(defn main []
  (doseq [[url headers expected]
          [["http://localhost:8787/sync/example/snapshot/download" {} "http://localhost:8787"]
           ["http://203.0.113.10:8443/sync/example/snapshot/download"
            {"x-forwarded-proto" "https"} "https://203.0.113.10:8443"]
           ["https://example.com/sync/example/snapshot/download" {} "https://example.com"]
           ["http://localhost:8787/" {"x-forwarded-proto" "javascript"} "http://localhost:8787"]
           ["http://203.0.113.10:8443/" {"x-forwarded-proto" "https" "x-forwarded-host" "evil.invalid"}
            "https://203.0.113.10:8443"]]]
    (assert (= expected (common/public-origin (js/Request. url #js {:headers (clj->js headers)})))
            (str "Incorrect public origin for " url)))
  (println "PASS upstream public-origin: HTTPS proxy, direct HTTP, native HTTPS, and untrusted headers"))
