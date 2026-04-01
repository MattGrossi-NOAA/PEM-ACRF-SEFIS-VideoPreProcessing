# PEM-ACRF-SEFIS-VideoPreProcessing

Stitching and clipping SEFIS videos for automated analysis

- Folders are named using a combination of a project code, year, collection, and camera. E.g., `T60250001_A`. We typically call this the "collection number" for short. 

- Each folder is a single deployment of a trap with a camera. Each folder contains a board file and a number of underwater video files. 

- There is a csv file with each unique identifying collection number in a column, and the `timeonbottom` time, which is the elapsed time from when the video files start to when the trap lands on the bottom. 

- We want to clip out a segment of video starting exactly 8 minutes after the trap lands on bottom and ending 32 minutes after the trap lands on bottom, for a 24-min video clip in total. This will involve stitching files together as well. 

- This 24-min video clip should be named exactly like the folder containing the files (e.g., `T60250001_A`)

More details to come as the repository is built out.

## Disclaimer

This repository is a scientific product and is not official communication of the National Oceanic and Atmospheric Administration, or the United States Department of Commerce. All NOAA GitHub project code is provided on an ‘as is’ basis and the user assumes responsibility for its use. Any claims against the Department of Commerce or Department of Commerce bureaus stemming from the use of this GitHub project will be governed by all applicable Federal law. Any reference to specific commercial products, processes, or services by service mark, trademark, manufacturer, or otherwise, does not constitute or imply their endorsement, recommendation or favoring by the Department of Commerce. The Department of Commerce seal and logo, or the seal and logo of a DOC bureau, shall not be used in any manner to imply endorsement of any commercial product or activity by DOC or the United States Government.