import json
from pathlib import Path
import tempfile
import unittest

from training_window import improvement_text, loss_series, measurement_change, movie_result, run_history


class MovieTrainingResultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root/"runs"/"movie-run"
        self.run.mkdir(parents=True)
        (self.root/"logs").mkdir()
        self.record = {"kind":"movie-temporal","status":"running","state_label":"训练中",
                       "epochs":5,"sample_fps":1.0,"movie_count":2,"device":"CPU"}

    def write(self,name,value):
        (self.run/name).write_text(json.dumps(value),encoding="utf-8")

    def test_running_movie_has_live_epoch_metrics_without_image_metrics(self):
        self.write("movie_metrics.json",{
            "epochs":[{"epoch":2,"train_loss":0.2,"val_loss":0.3}],
            "total_epochs":5,"movie_count":2,"sample_count":500,"train_windows":100,
            "val_windows":30,"validation_kind":"independent-films","baseline_loss":0.4})
        view = movie_result(self.run,self.record,{})
        self.assertEqual((view["count"],view["limit"]),(2,5))
        self.assertIn("2 部电影",view["progress"])
        self.assertIn("500 个时间点",view["progress"])
        self.assertIn("独立电影验证",view["progress"])
        self.assertIn("验证损失 0.30000",view["metrics"])
        self.assertIn("基线 0.40000",view["metrics"])
        self.assertNotIn("训练图",view["progress"])
        self.assertNotIn("mAP",view["metrics"])

    def test_no_holdout_is_explicit_and_only_compares_training_baseline(self):
        self.write("movie_metrics.json",{
            "epochs":[{"epoch":1,"train_loss":0.2,"val_loss":None}],
            "train_windows":3,"val_windows":0,"validation_kind":"none",
            "baseline_loss":None,"train_baseline_loss":0.25})
        view = movie_result(self.run,self.record,{})
        self.assertIn("无验证数据",view["progress"])
        self.assertIn("训练基线 0.25000",view["metrics"])
        self.assertNotIn("验证损失",view["metrics"])

    def test_corrupt_partial_metrics_are_tolerated_during_extraction(self):
        (self.run/"movie_metrics.json").write_text('{"epochs": [',encoding="utf-8")
        view = movie_result(self.run,self.record,{})
        self.assertEqual(view["count"],0)
        self.assertEqual(view["metrics"],"")
        self.assertIn("读取电影",view["progress"])
        self.assertIn("不直接输出动画",view["capability"])

    def test_within_film_validation_does_not_claim_cross_film_quality(self):
        completed = {"kind":"movie-temporal","epochs":5,"movie_count":1,
                     "sample_count":600,"train_windows":400,"val_windows":100,
                     "validation_kind":"within-film","train_loss":0.3,"val_loss":0.4}
        view = movie_result(self.run,{**self.record,"status":"completed","state_label":"已完成"},completed)
        self.assertIn("尚未验证跨电影泛化",view["progress"])
        self.assertEqual(view["count"],5)
        self.assertIn("特征预测误差",view["metrics"])

    def test_completed_movie_kind_restored_without_a_job_log(self):
        self.write("completed.json",{"kind":"movie-temporal","epochs":5,"movie_count":3})
        record = run_history(self.root,None)["movie-run"]
        self.assertEqual(record["kind"],"movie-temporal")
        self.assertEqual(record["state_label"],"已完成")
        self.assertEqual(record["movie_count"],3)

    def test_improvement_handles_absent_zero_and_worse_baselines(self):
        self.assertEqual(improvement_text(None,.2),("无可比基线","neutral"))
        self.assertEqual(improvement_text(.1,None),("无可比基线","neutral"))
        self.assertEqual(improvement_text(float("nan"),.2),("无可比基线","neutral"))
        self.assertEqual(improvement_text(0,0),("同为零","neutral"))
        self.assertEqual(improvement_text(.1,0),("基线为零，误差增加","worse"))
        self.assertEqual(improvement_text(.15,.2),("误差降低 25.00%","better"))
        self.assertEqual(improvement_text(.3,.2),("误差增加 50.00%","worse"))

    def test_early_stop_displays_actual_rounds_and_selected_previous_model(self):
        self.write("movie_metrics.json",{
            "epochs":[{"epoch":3,"train_loss":.2,"val_loss":.3,"learning_rate":.0005}],
            "total_epochs":20,"actual_epochs":3,"early_stopped":True,
            "baseline_loss":.4,"validation_kind":"within-film",
            "warm_start_reason":"沿用上一版参数与标准化，继续学习新素材"})
        completed = {"kind":"movie-temporal","epochs":3,"actual_epochs":3,"best_epoch":0,
                     "best_origin":"previous-model","train_loss":.15,"val_loss":.2,
                     "early_stopped":True}
        view = movie_result(self.run,{**self.record,"state_label":"已完成"},completed)
        self.assertEqual((view["count"],view["limit"]),(3,20))
        self.assertIn("提前停止",view["progress"])
        self.assertIn("3/20 轮",view["progress"])
        self.assertIn("保留上一版模型",view["metrics"])
        self.assertIn("验证损失 0.20000",view["metrics"])
        self.assertIn("误差降低 50.00%",view["metrics"])
        self.assertIn("当前学习率 0.0005",view["metrics"])
        self.assertIn("继续学习新素材",view["capability"])

    def test_no_validation_improvement_is_labeled_as_training_only(self):
        self.write("movie_metrics.json",{
            "epochs":[{"epoch":1,"train_loss":0.,"val_loss":None}],
            "validation_kind":"none","train_baseline_loss":0.,"baseline_loss":None})
        view = movie_result(self.run,self.record,{})
        self.assertIn("训练集同为零",view["metrics"])
        self.assertNotIn("验证损失",view["metrics"])
        self.assertNotIn("降低",view["metrics"])

    def test_chart_ignores_missing_non_finite_and_malformed_values(self):
        metrics = {"epochs":[None,{"epoch":1,"train_loss":.2,"val_loss":None},
                             {"epoch":2,"train_loss":float("nan"),"val_loss":.3},
                             {"epoch":3,"train_loss":.1,"val_loss":float("inf")},
                             {"epoch":"bad","train_loss":.05}]}
        self.assertEqual(loss_series(metrics),{"train":[(1,.2),(3,.1)],"val":[(2,.3)]})
        self.assertEqual(loss_series({"epochs":3}),{"train":[],"val":[]})

    def test_missing_observations_cannot_appear_as_perfect_learning(self):
        self.assertEqual(measurement_change({"sample_count":0,"model_loss":0,"baseline_loss":0}),
                         ("无有效样本","neutral"))
        self.assertEqual(measurement_change({"sample_count":0,"model_loss":None,"baseline_loss":None}),
                         ("无有效样本","neutral"))
        self.assertEqual(measurement_change({"sample_count":12,"model_loss":.2,"baseline_loss":.4}),
                         ("误差降低 50.00%","better"))


if __name__ == "__main__":
    unittest.main()
