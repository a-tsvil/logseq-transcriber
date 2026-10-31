(ns logseq.db-sync.checklist-test
  (:require [datascript.core :as d]
            [logseq.db :as ldb]
            [logseq.db-sync.test-sql :as test-sql]
            [logseq.db.sqlite.export :as sqlite-export]
            [logseq.db-sync.worker.handler.sync :as sync]
            [logseq.outliner.property :as outliner-property]
            [promesa.core :as p]
            [logseq.db-sync.worker.handler.semantic :as semantic]
            [logseq.db-sync.worker.routes.semantic :as routes]))

(defn main []
  (doseq [day [20240229 20261029 20300102]]
    (assert (semantic/valid-journal-day? day)))
  (doseq [day [nil "20261029" 20260229 20261301 20261000 20261032 20261029.5]]
    (assert (not (semantic/valid-journal-day? day))))
  (let [spec (routes/openapi-document "https://issuer.invalid")]
    (assert (= "boolean" (get-in spec [:components :schemas :BlockTree :properties :task :type])))
    (assert (= "boolean" (get-in spec [:components :schemas :BlockTree :properties :checkbox :type])))
    (assert (= "integer" (get-in spec [:paths "/api/v1/graphs/{graph-id}/capture" :post :requestBody
                                      :content "application/json" :schema :properties :journal-day :type]))))
  (println "PASS journal checklist API: calendar validation and typed capture schema"))

(defn boolean-main []
  (-> (p/let [key (.generateKey js/crypto.subtle #js {:name "AES-GCM" :length 256} true #js ["encrypt" "decrypt"])]
        (p/loop [encrypted? false]
          (let [conn (sqlite-export/create-conn)
                ctx #js {:conn conn :sql (test-sql/make-sql) :schema-ready true :env #js {}}
                pack (fn [value]
                       (if encrypted?
                         (p/let [iv (js/crypto.getRandomValues (js/Uint8Array. 12))
                                 encrypted (.encrypt js/crypto.subtle #js {:name "AES-GCM" :iv iv} key
                                                     (.encode (js/TextEncoder.) (ldb/write-transit-str value)))]
                           (ldb/write-transit-str [iv (js/Uint8Array. encrypted)]))
                         (p/resolved value)))
                item-id (random-uuid)
                item #(d/entity @conn [:block/uuid item-id])
                request (fn [body]
                          (js/Request. (str "http://localhost/semantic/capture?graph-id=test&graph-e2ee=" encrypted?)
                            #js {:method "POST" :headers #js {"content-type" "application/json"}
                                 :body (js/JSON.stringify (clj->js body))}))]
            (p/let [title (pack "Buy milk")
                    prop-title (pack "Checked")
                    prop-name (pack "checked")
                    payload {:journal-day 20300102 :checkbox-property {:title prop-title :name prop-name}
                             :blocks [{:uuid (str item-id) :title title :checkbox true}]}
                    response (sync/handle-http ctx (request payload))
                    _ (do (assert (= 201 (.-status response)))
                          (let [property (d/entity @conn semantic/checklist-property)]
                            (assert (= :checkbox (:logseq.property/type property)))
                            (assert (= :block-left (:logseq.property/ui-position property))))
                          (assert (false? (get (item) semantic/checklist-property)))
                          (assert (nil? (:logseq.property/status (item))))
                          (assert (not (some #(= :logseq.class/Task (:db/ident %)) (:block/tags (item))))))
                    _ (outliner-property/set-block-property! conn (:db/id (item)) semantic/checklist-property true)
                    retried (sync/handle-http ctx (request payload))
                    _ (do (assert (= 201 (.-status retried)))
                          (assert (true? (get (item) semantic/checklist-property))))
                    _ (outliner-property/set-block-property! conn (:db/id (item)) semantic/checklist-property false)
                    _ (assert (false? (get (item) semantic/checklist-property)))
                    bad (when encrypted?
                          (sync/handle-http ctx (request {:journal-day 20300102
                                                        :checkbox-property {:title "Plain" :name "plain"}
                                                        :blocks [{:title "Plain" :checkbox true}]})))]
              (if encrypted?
                (do (assert (= 400 (.-status bad)))
                    (println "PASS boolean checklists: encrypted/plain, initially unchecked, no Task/status, retry preservation, toggling, encrypted-label enforcement"))
                (p/recur true))))))
      (p/catch (fn [error] (js/console.error error) (set! (.-exitCode js/process) 1)))))
