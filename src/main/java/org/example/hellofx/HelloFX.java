package org.example.hellofx;

import javafx.application.Application;
import javafx.fxml.FXMLLoader;
import javafx.scene.Parent;
import javafx.scene.Scene;
import javafx.stage.Stage;

import java.io.IOException;

public class HelloFX extends Application {

    @Override
    public void start(Stage stage) throws IOException {
        Parent root = new FXMLLoader(getClass().getResource("hellofx.fxml")).load();
        stage.setTitle("BankNifty Live Trading Dashboard");
        stage.setWidth(1600);
        stage.setHeight(900);
        stage.setMinWidth(1100);
        stage.setMinHeight(700);
        Scene scene = new Scene(root);
        stage.setScene(scene);
        stage.show();
    }

    @Override
    public void stop() {
        MainScheduler.shutdown();
    }

    public static void main(String[] args) {
        launch(args);
    }
}
