package org.example.hellofx;

import javafx.application.Application;
import javafx.fxml.FXMLLoader;
import javafx.scene.Parent;
import javafx.scene.Scene;
import javafx.scene.image.Image;
import javafx.stage.Stage;

import java.io.IOException;
import java.util.Objects;

public class HelloFX extends Application {

    @Override
    public void start(Stage stage) throws IOException {
        Parent root = new FXMLLoader(getClass().getResource("hellofx.fxml")).load();
        stage.setTitle("Hello World");
        stage.getIcons().add(new Image(Objects.requireNonNull(getClass().getResourceAsStream("/hellofx.png"))));
        stage.setScene(new Scene(root));
        stage.show();
    }

    public static void main(String[] args) {
        launch(args);
    }
}
