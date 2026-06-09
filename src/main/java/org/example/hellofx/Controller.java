package org.example.hellofx;

import javafx.fxml.FXML;
import javafx.scene.control.Label;

public class Controller {
    @FXML private Label helloLabel;

    private final String[] greetings = {"Hello", "What's up", "Howdy", "Yo"};
    private int currentGreetingIndex = 0;

    @FXML
    public void changeGreeting() {
        currentGreetingIndex = (currentGreetingIndex + 1) % greetings.length;
        helloLabel.setText(greetings[currentGreetingIndex]);
    }
}
