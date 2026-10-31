(ns logseq.db-sync.allowlist-test
  (:require [logseq.db-sync.worker.allowlist :as allowlist]
            [logseq.db-sync.worker.auth :as auth]
            [logseq.db-sync.proxy-origin-test :as proxy-test]
            [logseq.db-sync.checklist-test :as checklist-test]
            [logseq.common.authorization :as authorization]
            [promesa.core :as p]))

(defn main []
  (proxy-test/main)
  (checklist-test/main)
  (checklist-test/boolean-main)
  (let [emails (atom #{"brother@example.com"})
        closed (atom [])
        db #js {:prepare
                (fn [sql]
                  (let [params (atom [])
                        statement #js {}]
                    (aset statement "bind" (fn [& args] (reset! params args) statement))
                    (aset statement "all"
                          (fn []
                            (p/resolved
                             #js {:results
                                  (clj->js
                                   (cond
                                     (= sql "SELECT email, email_verified FROM users WHERE id = ?")
                                     (case (first @params)
                                       "owner-id" [{:email "owner@example.com" :email_verified 1}]
                                       "outsider-id" [{:email "outsider@example.com" :email_verified 1}]
                                       [])
                                     (= sql "SELECT email FROM training_allowed_accounts WHERE email = ?")
                                     (if (contains? @emails (first @params)) [{:email (first @params)}] [])
                                     (.startsWith sql "SELECT DISTINCT g.graph_id AS id")
                                     [{:id "shared-graph" :name "Shared graph"}]
                                     (= sql "SELECT email FROM training_allowed_accounts ORDER BY email")
                                     (mapv #(hash-map :email %) (sort @emails))
                                     (= sql "SELECT id FROM users WHERE lower(email) = ?")
                                     [{:id "brother-id"}]
                                     (.startsWith sql "SELECT graph_id FROM graph_members")
                                     [{:graph_id "shared-graph"}]
                                     :else []))})))
                    (aset statement "run"
                          (fn []
                            (if (.startsWith sql "INSERT")
                              (swap! emails conj (first @params))
                              (swap! emails disj (first @params)))
                            (p/resolved #js {:success true})))
                    statement))}
        secret (apply str (repeat 64 "s"))
        namespace #js {:idFromName identity
                       :get (fn [id]
                              (assert (= "shared-graph" id))
                              #js {:fetch (fn [request]
                                            (swap! closed conj (.-url request))
                                            (p/resolved (js/Response. "ok")))})}
        env #js {:DB db :TRAINING_OWNER_EMAIL "owner@example.com" :TRAINING_ALLOWLIST_TOKEN secret
                 :LOGSEQ_SYNC_DO namespace}
        owner #js {:sub "owner-id" :email "OWNER@example.com" :email_verified true}
        brother #js {:sub "brother-id" :email "brother@example.com" :email_verified true}
        outsider #js {:sub "outsider-id" :email "outsider@example.com" :email_verified true}
        request (fn [method payload token]
                  (js/Request. "http://internal/training/allowlist"
                               (clj->js (cond-> {:method method :headers {"authorization" (str "Bearer " token)}}
                                          payload (assoc :body (js/JSON.stringify (clj->js payload)))))))]
    (assert (= "brother@example.com" (allowlist/normalize-email " BROTHER@EXAMPLE.COM ")))
    (assert (nil? (allowlist/normalize-email "bad\n@example.com")))
    (-> (p/let [allowed (allowlist/<allowed-claims env owner)
                allowed-brother (allowlist/<allowed-claims env brother)
                denied (allowlist/<allowed-claims env outsider)
                unverified (allowlist/<allowed-claims env #js {:email "owner@example.com" :email_verified false})
                missing (allowlist/<allowed-claims env nil)
                pat (allowlist/<allowed-claims env #js {:sub "owner-id" :pat_id "test" :email "outsider@example.com"})
                rejected-pat (allowlist/<allowed-claims env #js {:sub "outsider-id" :pat_id "test" :email "owner@example.com"})]
          (assert (= owner allowed))
          (assert (= brother allowed-brother))
          (assert (nil? denied))
          (assert (nil? unverified))
          (assert (nil? missing))
          (assert (= "owner-id" (aget pat "sub")))
          (assert (nil? rejected-pat))
          (assert (= 401 (.-status (allowlist/handle (request "PUT" {:email "new@example.com"} "wrong") env))))
          (p/let [added (allowlist/handle (request "PUT" {:email "NEW@example.com"} secret) env)
                  allowed-new (allowlist/<allowed-claims env #js {:sub "new" :email "new@example.com" :email_verified true})
                  protected (allowlist/handle (request "DELETE" {:email "owner@example.com"} secret) env)
                  invalid (allowlist/handle (request "PUT" {:email "bad"} secret) env)
                  removed (allowlist/handle (request "DELETE" {:email "brother@example.com"} secret) env)
                  denied-brother (allowlist/<allowed-claims env brother)
                  graph-response (allowlist/handle (js/Request. "http://internal/training/graphs?email=owner%40example.com"
                                                               #js {:headers #js {:authorization (str "Bearer " secret)}}) env)
                  graph-body (.json graph-response)
                  wrong-method (allowlist/handle (js/Request. "http://internal/training/graphs"
                                                             #js {:method "DELETE" :headers #js {:authorization (str "Bearer " secret)}}) env)
                  denied-auth (with-redefs [authorization/verify-jwt (fn [_ _] (p/resolved outsider))]
                                (auth/auth-claims (js/Request. "http://internal/graphs" #js {:headers #js {:authorization "Bearer opaque"}}) env))]
            (assert (= 200 (.-status added)))
            (assert (some? allowed-new))
            (assert (= 400 (.-status protected)))
            (assert (= 400 (.-status invalid)))
            (assert (= 200 (.-status removed)))
            (assert (nil? denied-brother))
            (assert (nil? denied-auth))
            (assert (= "Shared graph" (aget (aget (aget graph-body "graphs") 0) "name")))
            (assert (= 405 (.-status wrong-method)))
            (assert (= ["http://internal/training/revoke?user-id=brother-id"] @closed))
            (println "PASS account allowlist: verified identities, PAT ownership, rejection, owner protection, add/remove")))
        (p/catch (fn [error] (js/console.error error) (set! (.-exitCode js/process) 1))))))
