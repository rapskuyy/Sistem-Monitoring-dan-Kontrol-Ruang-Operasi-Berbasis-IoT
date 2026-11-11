#!/usr/bin/env python3
"""train_rf.py - Random Forest Regression untuk prediksi tekanan/suhu ruang operasi.

Input : operating_room_ml_ready.csv (hasil clean_dataset.py) dengan kolom
        tempOut_C, presOut_hPa, blower, heater, tempIn_C, presIn_hPa,
        data_source, split (train/test).
Pakai : python train_rf.py                          # target presIn_hPa
        python train_rf.py --target tempIn_C        # target suhu
Output: folder rf_output/ berisi metrics.json, test_predictions.csv,
        rf_results.png, dan model_<target>.joblib
"""
import argparse, json, os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import joblib
from sklearn.ensemble import RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GridSearchCV, PredefinedSplit

FEATURES = ["tempOut_C", "presOut_hPa", "blower", "heater"]
OUTSIDE = {"presIn_hPa": "presOut_hPa", "tempIn_C": "tempOut_C"}   # untuk baseline
LEAKY = {"deltaPres_hPa", "deltaTemp_C", "elapsed_ms", "timestamp_local", "scenario",
         "presIn_hPa", "tempIn_C"}
GRID = {"n_estimators": [200, 400], "max_depth": [None, 10, 20],
        "min_samples_leaf": [1, 3, 5]}


def metrics(y, p):
    return {"MAE": float(mean_absolute_error(y, p)),
            "RMSE": float(np.sqrt(mean_squared_error(y, p))),
            "R2": float(r2_score(y, p))}


def load(path, target):
    df = pd.read_csv(path)
    if "data_source" not in df:
        df["data_source"] = "all"
    if "split" not in df:                      # tidak ada split -> kronologis 80/20
        k = int(len(df) * 0.8)
        df["split"] = ["train"] * k + ["test"] * (len(df) - k)
    df = df.dropna(subset=FEATURES + [target]).reset_index(drop=True)
    return df[df.split == "train"].reset_index(drop=True), df[df.split == "test"].reset_index(drop=True)


def blocked_folds(train, n_splits=5):
    """Fold berurutan (blok kontigu) per data_source; tanpa acak agar tidak bocor."""
    fold = np.zeros(len(train), dtype=int)
    for _, idx in train.groupby("data_source").indices.items():
        for f, part in enumerate(np.array_split(np.sort(idx), n_splits)):
            fold[part] = f
    return PredefinedSplit(fold)


def baselines(train, test, target):
    out = OUTSIDE[target]
    d_all = (train[target] - train[out]).mean()
    d_state = (train[target] - train[out]).groupby([train.blower, train.heater]).mean()
    key = list(zip(test.blower, test.heater))
    p_state = test[out].values + np.array([d_state.get(k, d_all) for k in key])
    return {"Baseline: rata-rata train": np.full(len(test), train[target].mean()),
            "Baseline: luar + selisih rata-rata per (blower,heater)": p_state}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="operating_room_ml_ready.csv")
    ap.add_argument("--target", default="presIn_hPa", choices=["presIn_hPa", "tempIn_C"])
    ap.add_argument("--out", default="rf_output")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    assert not (set(FEATURES) & LEAKY), "Fitur mengandung kolom bocor!"
    train, test = load(a.data, a.target)
    print(f"Target={a.target} | train={len(train)} | test={len(test)} | fitur={FEATURES}")

    # --- tuning dengan CV blok kronologis, skor MAE ---
    gs = GridSearchCV(RandomForestRegressor(random_state=a.seed, n_jobs=-1), GRID,
                      cv=blocked_folds(train), scoring="neg_mean_absolute_error", n_jobs=-1)
    gs.fit(train[FEATURES], train[a.target])
    rf = gs.best_estimator_
    print("Hyperparameter terbaik:", gs.best_params_,
          f"(CV MAE={-gs.best_score_:.4f})")

    # --- evaluasi pada data uji kronologis, per subset ---
    preds = {"Random Forest": rf.predict(test[FEATURES]), **baselines(train, test, a.target)}
    subsets = {"semua": np.ones(len(test), bool)}
    for s in sorted(test.data_source.unique()):
        subsets[s] = (test.data_source == s).values
    results = {name: {sub: metrics(test[a.target][m], p[m]) for sub, m in subsets.items()}
               for name, p in preds.items()}
    rows = [(n, s, r["MAE"], r["RMSE"], r["R2"]) for n, d in results.items() for s, r in d.items()]
    print("\n" + pd.DataFrame(rows, columns=["model", "subset", "MAE", "RMSE", "R2"])
          .round(4).to_string(index=False))

    # --- feature importance: impurity + permutation (pada data uji) ---
    perm = permutation_importance(rf, test[FEATURES], test[a.target], n_repeats=20,
                                  random_state=a.seed, scoring="neg_mean_absolute_error")
    imp = pd.DataFrame({"feature": FEATURES, "impurity": rf.feature_importances_,
                        "permutation_MAE_naik": perm.importances_mean,
                        "perm_std": perm.importances_std}).sort_values("impurity", ascending=False)
    print("\nFeature importance:\n", imp.round(4).to_string(index=False))

    # --- simpan ---
    pd.DataFrame({"data_source": test.data_source, "actual": test[a.target],
                  "pred": preds["Random Forest"]}).to_csv(f"{a.out}/test_predictions.csv", index=False)
    with open(f"{a.out}/metrics.json", "w") as f:
        json.dump({"target": a.target, "features": FEATURES, "best_params": gs.best_params_,
                   "cv_mae": -gs.best_score_, "results": results,
                   "importance": imp.to_dict("records")}, f, indent=2)
    joblib.dump(rf, f"{a.out}/model_{a.target}.joblib")

    # --- plot ---
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.4))
    y, p = test[a.target].values, preds["Random Forest"]
    for s in sorted(test.data_source.unique()):
        m = (test.data_source == s).values
        ax[0].scatter(y[m], p[m], s=14, alpha=.7, label=s)
    lim = [min(y.min(), p.min()), max(y.max(), p.max())]
    ax[0].plot(lim, lim, "k--", lw=1)
    ax[0].set(xlabel="Aktual", ylabel="Prediksi", title=f"Aktual vs prediksi ({a.target})")
    ax[0].legend()
    ax[1].hist(y - p, bins=30, color="#4c78a8")
    ax[1].axvline(0, color="k", lw=1)
    ax[1].set(xlabel="Residual (aktual - prediksi)", title="Distribusi residual")
    ax[2].barh(imp.feature[::-1], imp.permutation_MAE_naik[::-1],
               xerr=imp.perm_std[::-1], color="#f58518")
    ax[2].set(xlabel="Kenaikan MAE saat fitur diacak", title="Permutation importance (uji)")
    fig.tight_layout()
    fig.savefig(f"{a.out}/rf_results.png", dpi=130)
    print(f"\nTersimpan di {a.out}/")


if __name__ == "__main__":
    main()
